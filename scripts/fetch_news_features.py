from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401

from app.config.settings import get_settings
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import fetch_rss_headlines
from app.news.ticker_mapper import group_headlines_by_symbol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch RSS headlines and store feature-only news analysis.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional explicit symbols to map headlines against.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    if not settings.news_features_enabled or not settings.news_rss_enabled:
        print("news_fetch_skipped=true reason=news_pipeline_disabled")
        return

    symbols = args.symbols or settings.active_symbols
    headlines = fetch_rss_headlines(lookback_hours=settings.news_lookback_hours)
    grouped = group_headlines_by_symbol(
        headlines,
        known_symbols=symbols,
        max_headlines_per_symbol=settings.news_max_headlines_per_ticker,
    )
    store = NewsFeatureStore(settings)
    stored = 0
    for symbol, symbol_headlines in grouped.items():
        analysis = analyze_headlines(symbol, symbol_headlines, settings=settings)
        store.write_feature(
            {
                **analysis,
                "headlines": [headline.to_dict() for headline in symbol_headlines],
            }
        )
        stored += 1
    print(f"news_fetch_skipped=false headlines={len(headlines)} symbols_analyzed={stored}")


if __name__ == "__main__":
    main()
