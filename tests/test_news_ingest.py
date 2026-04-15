from __future__ import annotations

from datetime import datetime, timedelta, timezone

import feedparser
import httpx

import app.news.rss_ingest as rss_ingest_module
import scripts.fetch_news_features as fetch_news_features_script
from app.config.settings import Settings
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import NewsFetchResult, NewsHeadline, NewsSourceDefinition, fetch_configured_headlines
from app.news.ticker_mapper import SECCompanyTickerResolver, map_headline_to_symbols


def _build_settings(tmp_path, **overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "broker_mode": "mock",
        "trading_enabled": False,
        "log_dir": str(tmp_path / "logs"),
        "news_features_enabled": True,
        "news_rss_enabled": True,
        "news_llm_enabled": False,
        "news_lookback_hours": 24,
        "news_enable_source_diversity_features": True,
    }
    values.update(overrides)
    return Settings(**values)


class FixtureParser:
    def __init__(self, fixtures: dict[str, str]):
        self.fixtures = fixtures

    def parse(self, source: str):
        return feedparser.parse(self.fixtures[source])


def _rss_fixture(*, title: str, description: str, link: str, published_at: str, feed_title: str, author: str | None = None) -> str:
    author_line = f"<author>{author}</author>" if author else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{feed_title}</title>
    <item>
      <title>{title}</title>
      <description>{description}</description>
      <link>{link}</link>
      <pubDate>{published_at}</pubDate>
      {author_line}
    </item>
  </channel>
</rss>
"""


def test_benzinga_rss_fixture_parses_with_source_attribution(tmp_path) -> None:
    published_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    benzinga_url = "https://fixtures.local/benzinga.xml"
    parser = FixtureParser(
        {
            benzinga_url: _rss_fixture(
                title="$AAPL surges after Benzinga alert",
                description="Apple shares rally on volume expansion.",
                link="https://example.com/benzinga/aapl",
                published_at=published_at,
                feed_title="Benzinga Wire",
            )
        }
    )
    settings = _build_settings(
        tmp_path,
        news_source_ids=["benzinga"],
        benzinga_rss_enabled=True,
        benzinga_rss_urls=[benzinga_url],
    )

    result = fetch_configured_headlines(settings=settings, parser=parser)

    assert result.total_fetched == 1
    assert result.total_deduped == 1
    item = result.deduped_items[0]
    assert item.source_id == "benzinga"
    assert item.source_type == "benzinga"
    assert item.source_name == "Benzinga Wire"
    assert "AAPL" in item.symbol_candidates


def test_marketwatch_redirect_succeeds_with_follow_redirects(tmp_path) -> None:
    start_url = "https://feeds.marketwatch.com/marketwatch/topstories/"
    final_url = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
    published_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == start_url:
            return httpx.Response(301, headers={"Location": final_url})
        assert str(request.url) == final_url
        return httpx.Response(
            200,
            text=_rss_fixture(
                title="$MSFT rises in MarketWatch top story",
                description="Microsoft gets a broad market mention.",
                link="https://example.com/marketwatch/msft",
                published_at=published_at,
                feed_title="MarketWatch Top Stories",
            ),
        )

    settings = _build_settings(
        tmp_path,
        news_source_ids=["marketwatch"],
        reuters_rss_urls=[],
        marketwatch_rss_urls=[start_url],
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_configured_headlines(settings=settings, client=client)

    assert result.total_fetched == 1
    assert result.source_health["marketwatch"]["success_count"] == 1
    assert result.source_health["marketwatch"]["failure_count"] == 0
    assert result.deduped_items[0].source_id == "marketwatch"


def test_benzinga_404_is_degraded_not_fatal(tmp_path) -> None:
    benzinga_url = "https://www.benzinga.example/missing-feed"
    settings = _build_settings(
        tmp_path,
        news_source_ids=["benzinga"],
        benzinga_rss_enabled=True,
        benzinga_rss_urls=[benzinga_url],
        news_fetch_retry_count=0,
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text="not found"))) as client:
        result = fetch_configured_headlines(settings=settings, client=client)

    assert result.total_fetched == 0
    assert result.degraded is True
    assert result.errors[0]["source_id"] == "benzinga"
    assert result.source_health["benzinga"]["failure_count"] == 1
    assert result.source_health["benzinga"]["degraded"] is True


def test_reuters_network_failure_is_captured_in_source_health(tmp_path) -> None:
    reuters_url = "https://feeds.reuters.example/reuters/businessNews"
    settings = _build_settings(
        tmp_path,
        news_source_ids=["reuters"],
        reuters_rss_urls=[reuters_url],
        marketwatch_rss_urls=[],
        news_fetch_retry_count=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_configured_headlines(settings=settings, client=client)

    assert result.total_fetched == 0
    assert result.degraded is True
    assert "name resolution failed" in result.source_health["reuters"]["last_error"]
    assert result.source_health["reuters"]["success_count"] == 0
    assert result.source_health["reuters"]["failure_count"] == 1


def test_sec_rss_fixture_parses_structured_metadata(tmp_path) -> None:
    published_at = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    sec_url = "https://fixtures.local/sec.xml"
    parser = FixtureParser(
        {
            sec_url: _rss_fixture(
                title="8-K - Example Corp",
                description="Current report filing from Example Corp.",
                link=(
                    "https://www.sec.gov/Archives/edgar/data/1234567/"
                    "0001234567-26-000001/example-8k.htm?accession_number=0001234567-26-000001"
                ),
                published_at=published_at,
                feed_title="SEC EDGAR Filings",
                author="Example Corp",
            )
        }
    )
    settings = _build_settings(
        tmp_path,
        news_source_ids=["sec"],
        sec_rss_enabled=True,
        sec_rss_urls=[sec_url],
        sec_user_agent="MoneyBot/1.0 support@example.com",
    )

    result = fetch_configured_headlines(settings=settings, parser=parser)

    assert result.total_fetched == 1
    item = result.deduped_items[0]
    assert item.source_id == "sec"
    assert item.source_type == "sec"
    assert item.raw_metadata["form_type"] == "8-K"
    assert item.raw_metadata["company_name"] == "Example Corp"
    assert item.raw_metadata["cik"] == "1234567"
    assert item.raw_metadata["accession"] == "0001234567-26-000001"
    assert item.raw_metadata["filing_date"] is not None


def test_sec_headline_with_cik_maps_to_known_ticker() -> None:
    resolver = SECCompanyTickerResolver.from_records(
        [{"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}]
    )
    headline = NewsHeadline(
        title="8-K - Apple Inc.",
        summary="Current report filing.",
        source="SEC EDGAR",
        source_id="sec",
        source_name="SEC EDGAR",
        source_type="sec",
        url="https://www.sec.gov/Archives/edgar/data/0000320193/example.htm",
        published_at="2026-04-14T14:00:00Z",
        raw_metadata={"cik": "0000320193", "company_name": "Unmatched Name"},
    )

    assert map_headline_to_symbols(headline, known_symbols=["AAPL"], sec_resolver=resolver) == ["AAPL"]


def test_sec_headline_with_company_name_maps_to_known_ticker() -> None:
    resolver = SECCompanyTickerResolver.from_records(
        [{"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."}]
    )
    headline = NewsHeadline(
        title="10-Q - Alphabet Inc.",
        summary="Quarterly report filing.",
        source="SEC EDGAR",
        source_id="sec",
        source_name="SEC EDGAR",
        source_type="sec",
        url="https://www.sec.gov/Archives/edgar/data/1652044/example.htm",
        published_at="2026-04-14T14:00:00Z",
        raw_metadata={"company_name": "Alphabet Inc."},
    )

    assert map_headline_to_symbols(headline, known_symbols=["GOOGL"], sec_resolver=resolver) == ["GOOGL"]


def test_multi_source_fetch_dedupes_across_sources(tmp_path, monkeypatch) -> None:
    reuters_url = "https://fixtures.local/reuters.xml"
    benzinga_url = "https://fixtures.local/benzinga.xml"
    reuters_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    benzinga_time = (datetime.now(timezone.utc) - timedelta(minutes=8)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    parser = FixtureParser(
        {
            reuters_url: _rss_fixture(
                title="AAPL jumps after filing update",
                description="Liquidity remains strong.",
                link="https://example.com/reuters/aapl",
                published_at=reuters_time,
                feed_title="Reuters Business",
            ),
            benzinga_url: _rss_fixture(
                title="AAPL jumps after filing update",
                description="Benzinga confirms the catalyst.",
                link="https://example.com/benzinga/aapl-confirmation",
                published_at=benzinga_time,
                feed_title="Benzinga Wire",
            ),
        }
    )
    monkeypatch.setattr(
        rss_ingest_module,
        "DEFAULT_RSS_SOURCES",
        [
            NewsSourceDefinition(
                source_id="reuters",
                source_name="Reuters Business",
                source_type="default_rss",
                urls=[reuters_url],
            )
        ],
    )
    settings = _build_settings(
        tmp_path,
        news_source_ids=["reuters", "benzinga"],
        reuters_rss_urls=[reuters_url],
        benzinga_rss_enabled=True,
        benzinga_rss_urls=[benzinga_url],
        news_dedupe_window_minutes=30,
    )

    result = fetch_configured_headlines(settings=settings, parser=parser)

    assert result.total_fetched == 2
    assert result.total_deduped == 1
    assert result.duplicate_count == 1
    assert set(result.source_ids) == {"reuters", "benzinga"}
    assert result.deduped_items[0].source_id == "benzinga"


def test_fetch_summary_degraded_when_headlines_do_not_group_to_symbols(tmp_path, monkeypatch) -> None:
    headline = NewsHeadline(
        title="Macro story without tracked ticker",
        summary="No symbol token is present.",
        source="MarketWatch Top Stories",
        source_id="marketwatch",
        source_name="MarketWatch Top Stories",
        source_type="default_rss",
        url="https://example.com/macro",
        published_at="2026-04-14T14:00:00Z",
    )
    fetch_result = NewsFetchResult(
        items=[headline],
        deduped_items=[headline],
        fetched_count_by_source={"marketwatch": 1},
        deduped_count_by_source={"marketwatch": 1},
        errors=[],
        source_ids=["marketwatch"],
        source_health={
            "marketwatch": {
                "source_id": "marketwatch",
                "configured_urls": ["https://example.com/mw.xml"],
                "success_count": 1,
                "failure_count": 0,
                "last_error": None,
                "degraded": False,
            }
        },
        degraded=False,
        degraded_reasons=[],
    )
    settings = _build_settings(tmp_path, default_symbols=["AAPL"], log_dir=str(tmp_path / "logs"))
    monkeypatch.setattr(fetch_news_features_script, "fetch_configured_headlines", lambda _settings: fetch_result)

    summary = fetch_news_features_script.run_news_feature_refresh(settings, symbols=["AAPL"])

    assert summary["news_pipeline_status"] == "degraded"
    assert summary["symbols_grouped"] == 0
    assert summary["symbols_analyzed"] == 0
    assert "deduped_headlines_without_symbol_mapping" in summary["degraded_reasons"]


def test_llm_disabled_news_analysis_still_emits_source_aware_features(tmp_path) -> None:
    settings = _build_settings(
        tmp_path,
        news_llm_enabled=False,
    )
    headlines = [
        rss_ingest_module.NewsHeadline(
            title="$AAPL rallies after Benzinga catalyst alert",
            summary="Benzinga notes strong relative volume.",
            source="Benzinga Wire",
            source_id="benzinga",
            source_name="Benzinga Wire",
            source_type="benzinga",
            url="https://example.com/benzinga/aapl",
            published_at="2026-04-14T14:00:00Z",
            symbol_candidates=["AAPL"],
        ),
        rss_ingest_module.NewsHeadline(
            title="8-K - Apple Inc.",
            summary="Current report filing from Apple Inc.",
            source="SEC EDGAR",
            source_id="sec",
            source_name="SEC EDGAR",
            source_type="sec",
            url=(
                "https://www.sec.gov/Archives/edgar/data/320193/"
                "0000320193-26-000010/aapl-8k.htm?accession_number=0000320193-26-000010"
            ),
            published_at="2026-04-14T14:05:00Z",
            symbol_candidates=["AAPL"],
            raw_metadata={"form_type": "8-K", "company_name": "Apple Inc."},
        ),
    ]

    analysis = analyze_headlines("AAPL", headlines, settings=settings)
    store = NewsFeatureStore(settings)
    store.write_feature({**analysis, "headlines": [headline.to_dict() for headline in headlines]})
    latest = store.latest_for_symbol("AAPL")

    assert analysis["analysis_mode"] == "heuristic"
    assert analysis["llm_status"] == "news_llm_disabled"
    assert analysis["benzinga_headline_count"] == 1
    assert analysis["source_diversity_count"] == 2
    assert analysis["cross_source_confirmation"] is True
    assert analysis["sec_event_flag"] is True
    assert analysis["sec_form_type"] == "8-K"
    assert analysis["catalyst_score"] > 0.0
    assert latest is not None
    assert latest["source_diversity_count"] == 2
