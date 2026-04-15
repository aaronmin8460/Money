from __future__ import annotations

import argparse
import json
from collections import Counter

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.monitoring.logger import get_logger, init_logging
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import fetch_configured_headlines
from app.news.ticker_mapper import group_headlines_by_symbol

logger = get_logger("scripts.news_fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch RSS headlines and store feature-only news analysis.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional explicit symbols to map headlines against.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    init_logging()
    settings = get_settings()
    if not settings.news_features_enabled or not settings.news_rss_enabled:
        logger.info(
            "News feature refresh skipped",
            extra={
                "reason": "news_pipeline_disabled",
                "news_features_enabled": settings.news_features_enabled,
                "news_rss_enabled": settings.news_rss_enabled,
                "news_llm_status": settings.news_llm_status,
                "enabled_news_sources": settings.enabled_news_sources,
            },
        )
        print("news_fetch_skipped=true reason=news_pipeline_disabled")
        return

    symbols = args.symbols or settings.active_symbols
    logger.info(
        "News feature refresh starting",
        extra={
            "symbols_requested": symbols,
            "news_lookback_hours": settings.news_lookback_hours,
            "news_llm_status": settings.news_llm_status,
            "openai_model": settings.openai_model,
            "enabled_news_sources": settings.enabled_news_sources,
        },
    )
    fetch_result = fetch_configured_headlines(settings)
    headlines = fetch_result.deduped_items
    grouped = group_headlines_by_symbol(
        headlines,
        known_symbols=symbols,
        max_headlines_per_symbol=settings.news_max_headlines_per_ticker,
    )
    store = NewsFeatureStore(settings)
    stored = 0
    analysis_modes: Counter[str] = Counter()
    analysis_reasons: Counter[str] = Counter()
    for symbol, symbol_headlines in grouped.items():
        analysis = analyze_headlines(symbol, symbol_headlines, settings=settings)
        store.write_feature(
            {
                **analysis,
                "headlines": [headline.to_dict() for headline in symbol_headlines],
            }
        )
        analysis_modes[str(analysis.get("analysis_mode") or "unknown")] += 1
        analysis_reasons[str(analysis.get("analysis_reason") or "unspecified")] += 1
        stored += 1
    summary = {
        "news_fetch_skipped": False,
        "total_fetched": fetch_result.total_fetched,
        "total_deduped": fetch_result.total_deduped,
        "duplicate_count": fetch_result.duplicate_count,
        "fetched_count_by_source": fetch_result.fetched_count_by_source,
        "deduped_count_by_source": fetch_result.deduped_count_by_source,
        "source_errors": fetch_result.errors,
        "symbols_grouped": len(grouped),
        "symbols_analyzed": stored,
        "news_llm_status": settings.news_llm_status,
        "analysis_modes": dict(analysis_modes),
        "analysis_reasons": dict(analysis_reasons),
    }
    logger.info("News feature refresh completed", extra=summary)
    print(
        "news_fetch_skipped=false "
        f"total_fetched={fetch_result.total_fetched} "
        f"total_deduped={fetch_result.total_deduped} "
        f"duplicate_count={fetch_result.duplicate_count} "
        f"symbols_grouped={len(grouped)} "
        f"symbols_analyzed={stored} "
        f"news_llm_status={settings.news_llm_status} "
        f"fetched_count_by_source={json.dumps(fetch_result.fetched_count_by_source, sort_keys=True)} "
        f"deduped_count_by_source={json.dumps(fetch_result.deduped_count_by_source, sort_keys=True)} "
        f"source_errors={json.dumps(fetch_result.errors, sort_keys=True)} "
        f"analysis_modes={json.dumps(dict(analysis_modes), sort_keys=True)} "
        f"analysis_reasons={json.dumps(dict(analysis_reasons), sort_keys=True)}"
    )


if __name__ == "__main__":
    main()
