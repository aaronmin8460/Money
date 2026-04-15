from __future__ import annotations

import argparse
import json
from collections import Counter

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.monitoring.logger import get_logger, init_logging
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import fetch_rss_headlines
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
        },
    )
    headlines = fetch_rss_headlines(lookback_hours=settings.news_lookback_hours)
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
        "headlines": len(headlines),
        "symbols_grouped": len(grouped),
        "symbols_analyzed": stored,
        "news_llm_status": settings.news_llm_status,
        "analysis_modes": dict(analysis_modes),
        "analysis_reasons": dict(analysis_reasons),
    }
    logger.info("News feature refresh completed", extra=summary)
    print(
        "news_fetch_skipped=false "
        f"headlines={len(headlines)} "
        f"symbols_grouped={len(grouped)} "
        f"symbols_analyzed={stored} "
        f"news_llm_status={settings.news_llm_status} "
        f"analysis_modes={json.dumps(dict(analysis_modes), sort_keys=True)} "
        f"analysis_reasons={json.dumps(dict(analysis_reasons), sort_keys=True)}"
    )


if __name__ == "__main__":
    main()
