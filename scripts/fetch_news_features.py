from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - exercised by test imports
    from scripts import _bootstrap  # noqa: F401

from app.config.settings import Settings, get_settings
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import get_logger, init_logging
from app.news.feature_store import NewsFeatureStore
from app.news.llm_analysis import analyze_headlines
from app.news.rss_ingest import NewsFetchResult, fetch_configured_headlines
from app.news.ticker_mapper import group_headlines_by_symbol

logger = get_logger("scripts.news_fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch RSS headlines and store feature-only news analysis.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional explicit symbols to map headlines against.")
    return parser


def derive_news_pipeline_status(
    fetch_result: NewsFetchResult,
    *,
    symbols_grouped: int,
    symbols_analyzed: int,
) -> tuple[str, list[str]]:
    reasons = list(fetch_result.degraded_reasons)
    if fetch_result.total_deduped > 0 and symbols_grouped == 0:
        reasons.append("deduped_headlines_without_symbol_mapping")
    if symbols_grouped > 0 and symbols_analyzed == 0:
        reasons.append("grouped_symbols_without_analysis")

    source_health = fetch_result.source_health or {}
    all_configured_sources_failed = bool(source_health) and all(
        int(health.get("success_count") or 0) == 0 and int(health.get("failure_count") or 0) > 0
        for health in source_health.values()
    )
    if all_configured_sources_failed and fetch_result.total_deduped == 0:
        return "failed", list(dict.fromkeys([*reasons, "all_configured_sources_failed"]))
    if fetch_result.errors and not fetch_result.source_ids:
        return "failed", list(dict.fromkeys([*reasons, "news_fetch_unavailable"]))
    if fetch_result.degraded or reasons:
        return "degraded", list(dict.fromkeys(reasons))
    return "healthy", []


def _pipeline_state_path(settings: Settings) -> Path:
    return Path(settings.log_dir) / "news_pipeline_state.json"


def _record_pipeline_status(settings: Settings, *, status: str) -> dict[str, Any]:
    path = _pipeline_state_path(settings)
    previous: dict[str, Any] = {}
    try:
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read news pipeline state: %s", exc)

    previous_count = int(previous.get("consecutive_degraded_runs") or 0)
    degraded_like = status in {"degraded", "failed"}
    consecutive = previous_count + 1 if degraded_like else 0
    state = {
        "last_status": status,
        "consecutive_degraded_runs": consecutive,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist news pipeline state: %s", exc)
    return state


def _send_degraded_notification(settings: Settings, *, summary: dict[str, Any], state: dict[str, Any]) -> None:
    if summary["news_pipeline_status"] not in {"degraded", "failed"}:
        return
    if int(state.get("consecutive_degraded_runs") or 0) < 2:
        return
    notifier = get_discord_notifier(settings)
    notifier.send_system_notification(
        event="News pipeline degraded",
        reason=f"{summary['news_pipeline_status']} for {state['consecutive_degraded_runs']} consecutive runs",
        details={
            "news_pipeline_status": summary["news_pipeline_status"],
            "degraded_reasons": summary["degraded_reasons"],
            "total_deduped": summary["total_deduped"],
            "symbols_grouped": summary["symbols_grouped"],
            "symbols_analyzed": summary["symbols_analyzed"],
            "source_health": summary["source_health"],
        },
        category="news_pipeline",
    )


def run_news_feature_refresh(settings: Settings, *, symbols: list[str]) -> dict[str, Any]:
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

    pipeline_status, degraded_reasons = derive_news_pipeline_status(
        fetch_result,
        symbols_grouped=len(grouped),
        symbols_analyzed=stored,
    )
    summary = {
        "news_fetch_skipped": False,
        "news_pipeline_status": pipeline_status,
        "degraded_reasons": degraded_reasons,
        "total_fetched": fetch_result.total_fetched,
        "total_deduped": fetch_result.total_deduped,
        "duplicate_count": fetch_result.duplicate_count,
        "fetched_count_by_source": fetch_result.fetched_count_by_source,
        "deduped_count_by_source": fetch_result.deduped_count_by_source,
        "source_errors": fetch_result.errors,
        "source_health": fetch_result.source_health,
        "symbols_grouped": len(grouped),
        "symbols_analyzed": stored,
        "news_llm_status": settings.news_llm_status,
        "analysis_modes": dict(analysis_modes),
        "analysis_reasons": dict(analysis_reasons),
    }
    state = _record_pipeline_status(settings, status=pipeline_status)
    summary["consecutive_degraded_runs"] = int(state.get("consecutive_degraded_runs") or 0)
    _send_degraded_notification(settings, summary=summary, state=state)
    return summary


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
    summary = run_news_feature_refresh(settings, symbols=symbols)
    if summary["news_pipeline_status"] == "healthy":
        logger.info("News feature refresh completed", extra=summary)
    elif summary["news_pipeline_status"] == "degraded":
        logger.warning("News feature refresh completed in degraded state", extra=summary)
    else:
        logger.error("News feature refresh failed", extra=summary)
    print(
        "news_fetch_skipped=false "
        f"news_pipeline_status={summary['news_pipeline_status']} "
        f"total_fetched={summary['total_fetched']} "
        f"total_deduped={summary['total_deduped']} "
        f"duplicate_count={summary['duplicate_count']} "
        f"symbols_grouped={summary['symbols_grouped']} "
        f"symbols_analyzed={summary['symbols_analyzed']} "
        f"news_llm_status={settings.news_llm_status} "
        f"degraded_reasons={json.dumps(summary['degraded_reasons'], sort_keys=True)} "
        f"fetched_count_by_source={json.dumps(summary['fetched_count_by_source'], sort_keys=True)} "
        f"deduped_count_by_source={json.dumps(summary['deduped_count_by_source'], sort_keys=True)} "
        f"source_health={json.dumps(summary['source_health'], sort_keys=True)} "
        f"source_errors={json.dumps(summary['source_errors'], sort_keys=True)} "
        f"analysis_modes={json.dumps(summary['analysis_modes'], sort_keys=True)} "
        f"analysis_reasons={json.dumps(summary['analysis_reasons'], sort_keys=True)}"
    )


if __name__ == "__main__":
    main()
