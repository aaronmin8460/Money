from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config.settings import Settings, get_settings
from app.monitoring.logger import get_logger

logger = get_logger("news.rss")

_SEC_PACE_SECONDS = 0.5
_FORM_TYPE_PATTERN = re.compile(
    r"\b(8-K|10-K|10-Q|S-1(?:/A)?|424B[1-9]|13D(?:/A)?|13G(?:/A)?|SC 13D|SC 13G|6-K)\b",
    re.IGNORECASE,
)
_CIK_PATTERN = re.compile(r"/data/0*([0-9]{4,10})/")
_ACCESSION_PATTERN = re.compile(r"accession(?:_number)?[=/]([0-9-]{10,})", re.IGNORECASE)
_SYMBOL_PATTERN = re.compile(r"\$?[A-Z]{1,5}(?:/[A-Z]{3,4})?")


@dataclass(frozen=True)
class NewsSourceDefinition:
    source_id: str
    source_name: str
    source_type: str
    urls: list[str]
    user_agent: str | None = None
    pace_seconds: float = 0.0


@dataclass
class NewsSourceHealth:
    source_id: str
    configured_urls: list[str]
    success_count: int = 0
    failure_count: int = 0
    last_error: str | None = None
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "configured_urls": list(self.configured_urls),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "degraded": self.degraded,
        }


@dataclass
class NewsHeadline:
    title: str
    summary: str
    source: str
    url: str
    published_at: str
    source_id: str = ""
    source_name: str = ""
    source_type: str = "rss"
    symbol_candidates: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_source_name = self.source_name or self.source or "unknown"
        normalized_source_id = self.source_id or _slugify_source_id(normalized_source_name)
        object.__setattr__(self, "source_name", normalized_source_name)
        object.__setattr__(self, "source", normalized_source_name)
        object.__setattr__(self, "source_id", normalized_source_id)
        object.__setattr__(
            self,
            "symbol_candidates",
            [str(symbol).strip().upper() for symbol in self.symbol_candidates if str(symbol).strip()],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": self.source_type,
            "url": self.url,
            "published_at": self.published_at,
            "symbol_candidates": list(self.symbol_candidates),
            "raw_metadata": dict(self.raw_metadata),
        }


@dataclass
class NewsFetchResult:
    items: list[NewsHeadline]
    deduped_items: list[NewsHeadline]
    fetched_count_by_source: dict[str, int]
    deduped_count_by_source: dict[str, int]
    errors: list[dict[str, str]]
    source_ids: list[str]
    source_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    degraded: bool = False
    degraded_reasons: list[str] = field(default_factory=list)

    @property
    def total_fetched(self) -> int:
        return len(self.items)

    @property
    def total_deduped(self) -> int:
        return len(self.deduped_items)

    @property
    def duplicate_count(self) -> int:
        return max(0, self.total_fetched - self.total_deduped)

    def to_summary(self) -> dict[str, Any]:
        return {
            "total_fetched": self.total_fetched,
            "total_deduped": self.total_deduped,
            "duplicate_count": self.duplicate_count,
            "fetched_count_by_source": dict(self.fetched_count_by_source),
            "deduped_count_by_source": dict(self.deduped_count_by_source),
            "errors": list(self.errors),
            "source_ids": list(self.source_ids),
            "source_health": dict(self.source_health),
            "degraded": self.degraded,
            "degraded_reasons": list(self.degraded_reasons),
        }


DEFAULT_RSS_SOURCES = [
    NewsSourceDefinition(
        source_id="marketwatch",
        source_name="MarketWatch Top Stories",
        source_type="default_rss",
        urls=["https://feeds.marketwatch.com/marketwatch/topstories/"],
    ),
]


def _slugify_source_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return normalized or "unknown"


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, IndexError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _extract_symbol_candidates(title: str, summary: str) -> list[str]:
    text = f"{title} {summary}".upper()
    return sorted({token.lstrip("$").upper() for token in _SYMBOL_PATTERN.findall(text)})


def _extract_sec_metadata(entry: Any, title: str, summary: str, url: str) -> dict[str, Any]:
    form_match = _FORM_TYPE_PATTERN.search(title) or _FORM_TYPE_PATTERN.search(summary)
    form_type = form_match.group(1).upper().replace("SC ", "SC_") if form_match else None
    company_name = str(getattr(entry, "author", "")).strip() or None
    if company_name is None and " - " in title:
        title_parts = [part.strip() for part in title.split(" - ") if part.strip()]
        if len(title_parts) >= 2:
            company_name = title_parts[-1]
    cik_match = _CIK_PATTERN.search(url)
    accession_match = _ACCESSION_PATTERN.search(url)
    filing_date = None
    published = _parse_published(getattr(entry, "published", None) or getattr(entry, "updated", None))
    if published is not None:
        filing_date = published.date().isoformat()
    return {
        "form_type": form_type,
        "company_name": company_name,
        "cik": cik_match.group(1) if cik_match else None,
        "accession": accession_match.group(1) if accession_match else None,
        "filing_date": filing_date,
    }


def _headline_from_entry(entry: Any, source: NewsSourceDefinition, source_name: str) -> NewsHeadline | None:
    title = str(getattr(entry, "title", "")).strip()
    summary = str(
        getattr(entry, "summary", None)
        or getattr(entry, "description", None)
        or getattr(entry, "subtitle", None)
        or ""
    ).strip()
    url = str(getattr(entry, "link", "")).strip()
    published_at = _parse_published(getattr(entry, "published", None) or getattr(entry, "updated", None))
    raw_metadata = {
        "entry_id": str(getattr(entry, "id", "")).strip() or None,
        "source_url": url,
    }
    if source.source_type == "sec":
        raw_metadata.update(_extract_sec_metadata(entry, title, summary, url))

    headline = NewsHeadline(
        title=title,
        summary=summary,
        source=source_name,
        source_id=source.source_id,
        source_name=source_name,
        source_type=source.source_type,
        url=url,
        published_at=(published_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        symbol_candidates=_extract_symbol_candidates(title, summary),
        raw_metadata=raw_metadata,
    )
    if not headline.title and not headline.summary:
        return None
    return headline


def _filter_recent(items: list[NewsHeadline], *, lookback_hours: int) -> list[NewsHeadline]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent: list[NewsHeadline] = []
    for item in items:
        published_at = _parse_iso(item.published_at)
        if published_at is not None and published_at < cutoff:
            continue
        recent.append(item)
    return recent


def _parse_feed_entries(parsed: Any, source: NewsSourceDefinition) -> list[NewsHeadline]:
    source_name = str(getattr(getattr(parsed, "feed", None), "title", "")).strip() or source.source_name
    headlines: list[NewsHeadline] = []
    for entry in getattr(parsed, "entries", []):
        headline = _headline_from_entry(entry, source, source_name)
        if headline is not None:
            headlines.append(headline)
    return headlines


def _fetch_feed_body(
    url: str,
    *,
    timeout_seconds: float,
    retry_count: int,
    backoff_seconds: float,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    headers = {"User-Agent": user_agent} if user_agent else {}
    attempt = 0
    last_error: Exception | None = None
    while attempt <= retry_count:
        try:
            if client is None:
                response = httpx.get(url, timeout=timeout_seconds, headers=headers, follow_redirects=True)
            else:
                response = client.get(url, timeout=timeout_seconds, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # pragma: no cover - network failures are mocked in tests
            last_error = exc
            if attempt >= retry_count:
                break
            time.sleep(max(0.0, backoff_seconds * (attempt + 1)))
        finally:
            attempt += 1
    assert last_error is not None
    raise last_error


def dedupe_headlines(headlines: list[NewsHeadline], *, window_minutes: int) -> list[NewsHeadline]:
    if not headlines:
        return []
    ordered = sorted(
        headlines,
        key=lambda item: _parse_iso(item.published_at) or datetime.now(timezone.utc),
        reverse=True,
    )
    seen_urls: set[str] = set()
    accepted: list[NewsHeadline] = []
    title_index: dict[str, list[datetime]] = {}
    dedupe_window = timedelta(minutes=max(0, window_minutes))

    for item in ordered:
        normalized_url = _normalize_text(item.url)
        if normalized_url and normalized_url in seen_urls:
            continue

        normalized_title = _normalize_text(item.title)
        published_at = _parse_iso(item.published_at) or datetime.now(timezone.utc)
        existing_times = title_index.get(normalized_title, [])
        if any(abs((published_at - existing_at).total_seconds()) <= dedupe_window.total_seconds() for existing_at in existing_times):
            continue

        accepted.append(item)
        if normalized_url:
            seen_urls.add(normalized_url)
        title_index.setdefault(normalized_title, []).append(published_at)
    return list(reversed(accepted))


def fetch_rss_headlines(
    feeds: list[str] | None = None,
    *,
    lookback_hours: int = 24,
    parser: Any | None = None,
) -> list[NewsHeadline]:
    try:
        feedparser = parser
        if feedparser is None:
            import feedparser as feedparser_module  # type: ignore[import-not-found]

            feedparser = feedparser_module
    except Exception as exc:
        logger.warning("RSS ingestion disabled because feedparser is unavailable: %s", exc)
        return []

    headlines: list[NewsHeadline] = []
    for feed_url in feeds or [source.urls[0] for source in DEFAULT_RSS_SOURCES]:
        source_name = urlparse(feed_url).netloc or feed_url
        source = NewsSourceDefinition(
            source_id=_slugify_source_id(source_name),
            source_name=source_name,
            source_type="rss",
            urls=[feed_url],
        )
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
            continue
        headlines.extend(_parse_feed_entries(parsed, source))
    return _filter_recent(headlines, lookback_hours=lookback_hours)


def build_configured_news_sources(settings: Settings | None = None) -> list[NewsSourceDefinition]:
    resolved = settings or get_settings()
    if not resolved.news_rss_enabled:
        return []
    enabled_ids = set(resolved.news_source_ids)

    def _source_enabled(source_id: str, default_group: bool = False) -> bool:
        if not enabled_ids:
            return True
        return source_id in enabled_ids or (default_group and "default_rss" in enabled_ids)

    source_candidates = [
        NewsSourceDefinition(
            source_id="reuters",
            source_name="Reuters Business",
            source_type="default_rss",
            urls=list(resolved.reuters_rss_urls),
        ),
        NewsSourceDefinition(
            source_id="marketwatch",
            source_name="MarketWatch Top Stories",
            source_type="default_rss",
            urls=list(resolved.marketwatch_rss_urls),
        ),
    ]
    sources = [
        source
        for source in source_candidates
        if source.urls and _source_enabled(source.source_id, default_group=True)
    ]
    if resolved.benzinga_rss_enabled and resolved.benzinga_rss_urls and _source_enabled("benzinga"):
        sources.append(
            NewsSourceDefinition(
                source_id="benzinga",
                source_name="Benzinga RSS",
                source_type="benzinga",
                urls=list(resolved.benzinga_rss_urls),
            )
        )
    if resolved.sec_rss_enabled and resolved.sec_rss_urls and _source_enabled("sec"):
        sources.append(
            NewsSourceDefinition(
                source_id="sec",
                source_name="SEC EDGAR RSS",
                source_type="sec",
                urls=list(resolved.sec_rss_urls),
                user_agent=resolved.sec_user_agent,
                pace_seconds=_SEC_PACE_SECONDS,
            )
        )
    return sources


def _build_degraded_summary(
    *,
    source_ids: list[str],
    source_health: dict[str, NewsSourceHealth],
    deduped_count_by_source: Counter[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    productive_sources = sorted(source_id for source_id, count in deduped_count_by_source.items() if count > 0)
    if not source_ids:
        reasons.append("no_configured_news_sources")
    if len(productive_sources) < 2:
        reasons.append("fewer_than_two_sources_produced_headlines")

    non_sec_health = [health for source_id, health in source_health.items() if source_id != "sec"]
    if non_sec_health and all(health.success_count == 0 and health.failure_count > 0 for health in non_sec_health):
        reasons.append("all_non_sec_sources_failed")

    if source_ids and not productive_sources:
        reasons.append("no_sources_produced_headlines")

    return bool(reasons), reasons


def fetch_configured_headlines(
    settings: Settings | None = None,
    *,
    parser: Any | None = None,
    client: httpx.Client | None = None,
) -> NewsFetchResult:
    resolved = settings or get_settings()
    try:
        feedparser = parser
        if feedparser is None:
            import feedparser as feedparser_module  # type: ignore[import-not-found]

            feedparser = feedparser_module
    except Exception as exc:
        logger.warning("Configured news ingestion disabled because feedparser is unavailable: %s", exc)
        return NewsFetchResult([], [], {}, {}, [{"source_id": "feedparser", "error": str(exc)}], [])

    fetched_count_by_source: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    items: list[NewsHeadline] = []
    source_ids: list[str] = []
    source_health: dict[str, NewsSourceHealth] = {}

    for source in build_configured_news_sources(resolved):
        source_ids.append(source.source_id)
        health = NewsSourceHealth(source_id=source.source_id, configured_urls=list(source.urls))
        source_items: list[NewsHeadline] = []
        for index, url in enumerate(source.urls):
            try:
                if parser is not None:
                    parsed = feedparser.parse(url)
                else:
                    body = _fetch_feed_body(
                        url,
                        timeout_seconds=resolved.news_fetch_timeout_seconds,
                        retry_count=resolved.news_fetch_retry_count,
                        backoff_seconds=resolved.news_fetch_backoff_seconds,
                        user_agent=source.user_agent,
                        client=client,
                    )
                    parsed = feedparser.parse(body)
                source_items.extend(_parse_feed_entries(parsed, source))
                health.success_count += 1
                if source.pace_seconds > 0 and index < len(source.urls) - 1:
                    time.sleep(source.pace_seconds)
            except Exception as exc:
                health.failure_count += 1
                health.last_error = str(exc)
                logger.warning("News fetch failed for %s (%s): %s", source.source_id, url, exc)
                errors.append({"source_id": source.source_id, "url": url, "error": str(exc)})
                continue

        recent_items = _filter_recent(source_items, lookback_hours=resolved.news_lookback_hours)
        health.degraded = health.failure_count > 0 or health.success_count == 0
        source_health[source.source_id] = health
        fetched_count_by_source[source.source_id] += len(recent_items)
        logger.info(
            "Fetched source headlines",
            extra={
                "source_id": source.source_id,
                "source_type": source.source_type,
                "headline_count": len(recent_items),
                "url_count": len(source.urls),
                "source_health": health.to_dict(),
            },
        )
        items.extend(recent_items)

    deduped_items = dedupe_headlines(items, window_minutes=resolved.news_dedupe_window_minutes)
    deduped_count_by_source: Counter[str] = Counter(item.source_id for item in deduped_items)
    degraded, degraded_reasons = _build_degraded_summary(
        source_ids=source_ids,
        source_health=source_health,
        deduped_count_by_source=deduped_count_by_source,
    )
    log_extra = {
        "source_ids": source_ids,
        "total_fetched": len(items),
        "total_deduped": len(deduped_items),
        "duplicate_count": max(0, len(items) - len(deduped_items)),
        "fetched_count_by_source": dict(fetched_count_by_source),
        "deduped_count_by_source": dict(deduped_count_by_source),
        "error_count": len(errors),
        "source_health": {source_id: health.to_dict() for source_id, health in source_health.items()},
        "degraded": degraded,
        "degraded_reasons": degraded_reasons,
    }
    if degraded:
        logger.warning("Completed multi-source news fetch in degraded state", extra=log_extra)
    else:
        logger.info("Completed multi-source news fetch", extra=log_extra)
    logger.info(
        "News source health summary",
        extra=log_extra,
    )
    return NewsFetchResult(
        items=items,
        deduped_items=deduped_items,
        fetched_count_by_source=dict(fetched_count_by_source),
        deduped_count_by_source=dict(deduped_count_by_source),
        errors=errors,
        source_ids=source_ids,
        source_health={source_id: health.to_dict() for source_id, health in source_health.items()},
        degraded=degraded,
        degraded_reasons=degraded_reasons,
    )
