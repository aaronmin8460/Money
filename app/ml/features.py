from __future__ import annotations

import math
from typing import Any

from app.ml.schema import FEATURE_VERSION, SignalFeatureRow
from app.monitoring.events import build_signal_id
from app.strategies.base import EXIT_ORDER_INTENTS, Signal, TradeSignal

CATEGORICAL_FEATURES = [
    "model_purpose",
    "symbol",
    "asset_class",
    "strategy_name",
    "signal",
    "direction",
    "exit_stage",
    "regime",
    "session_state",
    "price_source_used",
    "news_sentiment_label",
    "news_sec_form_type",
]

NUMERIC_FEATURES = [
    "confidence",
    "entry",
    "stop",
    "target",
    "atr",
    "momentum",
    "liquidity",
    "spread",
    "strategy_score",
    "entry_ml_score",
    "risk_quality_adjustment",
    "reward_risk_ratio",
    "stop_distance_atr",
    "target_distance_atr",
    "breakout_distance_atr",
    "relative_volume",
    "rolling_volatility",
    "volatility_compression",
    "higher_timeframe_alignment",
    "latest_price",
    "latest_volume",
    "avg_volume",
    "dollar_volume",
    "scanner_signal_quality",
    "quote_age_seconds",
    "market_bullish_count",
    "market_bearish_count",
    "holding_duration_bars",
    "unrealized_return",
    "favorable_excursion_r",
    "adverse_excursion_r",
    "forward_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "realized_return",
    "risk_adjusted_return",
    "news_sentiment_score",
    "news_relevance_score",
    "news_risk_tags_count",
    "news_catalyst_score",
    "news_source_diversity_count",
    "news_cross_source_confirmation",
    "news_benzinga_headline_count",
    "news_sec_event_flag",
]


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _safe_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def resolve_model_purpose(signal: TradeSignal) -> str:
    metrics = signal.metrics or {}
    has_tracked_position = bool(
        metrics.get("has_tracked_position")
        or metrics.get("has_tracked_long_position")
        or metrics.get("has_sellable_long_position")
        or metrics.get("has_coverable_short_position")
    )
    is_exit_context = (
        signal.signal_type == "exit"
        or signal.reduce_only
        or bool(signal.exit_stage)
        or signal.order_intent in EXIT_ORDER_INTENTS
        or (signal.signal == Signal.HOLD and has_tracked_position)
    )
    return "exit" if is_exit_context else "entry"


def build_signal_feature_row(
    signal: TradeSignal,
    *,
    cycle_id: str | None = None,
    outcome_classification: str | None = None,
    latest_price: float | None = None,
    market_overview: dict[str, Any] | None = None,
    news_features: dict[str, Any] | None = None,
    label: int | None = None,
    label_source: str | None = None,
) -> SignalFeatureRow:
    metrics = signal.metrics or {}
    snapshot = metrics.get("normalized_snapshot", {}) if isinstance(metrics, dict) else {}
    pipeline = metrics.get("pipeline", {}) if isinstance(metrics, dict) else {}
    exit_state = metrics.get("exit_state", {}) if isinstance(metrics, dict) else {}
    buy_ranking = metrics.get("buy_ranking", {}) if isinstance(metrics, dict) else {}
    signal_id = str(metrics.get("signal_id") or build_signal_id(signal.symbol, signal.strategy_name, signal.generated_at))
    risk_tags = news_features.get("risk_tags") if isinstance(news_features, dict) else []
    if not isinstance(risk_tags, list):
        risk_tags = []

    regime_counts = market_overview or {}
    latest = latest_price if latest_price is not None else signal.price or signal.entry_price
    model_purpose = resolve_model_purpose(signal)
    trend_pipeline = pipeline.get("trend", {}) if isinstance(pipeline, dict) else {}
    volatility_pipeline = pipeline.get("volatility", {}) if isinstance(pipeline, dict) else {}
    liquidity_pipeline = pipeline.get("liquidity", {}) if isinstance(pipeline, dict) else {}

    return SignalFeatureRow(
        signal_id=signal_id,
        cycle_id=cycle_id or _safe_str(metrics.get("cycle_id")),
        generated_at=signal.generated_at.isoformat() if signal.generated_at else _safe_str(signal.timestamp),
        model_purpose=model_purpose,
        symbol=signal.symbol,
        asset_class=signal.asset_class.value,
        strategy_name=signal.strategy_name,
        signal=signal.signal.value,
        direction=signal.direction.value,
        exit_stage=_safe_str(signal.exit_stage),
        confidence=float(signal.confidence_score or 0.0),
        entry=_safe_float(signal.entry_price),
        stop=_safe_float(signal.stop_price),
        target=_safe_float(signal.target_price),
        atr=_safe_float(signal.atr),
        momentum=_safe_float(signal.momentum_score),
        liquidity=_safe_float(signal.liquidity_score),
        spread=_safe_float(metrics.get("spread_pct") or signal.spread_score),
        strategy_score=_safe_float(metrics.get("strategy_score") or signal.strength),
        entry_ml_score=_safe_float(buy_ranking.get("entry_ml_score") or (metrics.get("ml") or {}).get("score")),
        risk_quality_adjustment=_safe_float(buy_ranking.get("risk_quality_adjustment")),
        reward_risk_ratio=_safe_float(metrics.get("reward_risk_ratio")),
        stop_distance_atr=_safe_float(metrics.get("stop_distance_atr")),
        target_distance_atr=_safe_float(metrics.get("target_distance_atr")),
        breakout_distance_atr=_safe_float(metrics.get("breakout_distance_atr")),
        relative_volume=_safe_float(metrics.get("relative_volume") or liquidity_pipeline.get("relative_volume")),
        rolling_volatility=_safe_float(metrics.get("rolling_volatility") or volatility_pipeline.get("atr_pct")),
        volatility_compression=_safe_float(
            metrics.get("volatility_compression_ratio") or volatility_pipeline.get("compression_ratio")
        ),
        higher_timeframe_alignment=(
            1.0 if bool(trend_pipeline.get("higher_timeframe_aligned")) else 0.0
            if trend_pipeline
            else None
        ),
        regime=_safe_str(signal.regime_state),
        latest_price=_safe_float(latest),
        latest_volume=_safe_float(metrics.get("latest_volume")),
        avg_volume=_safe_float(metrics.get("avg_volume")),
        dollar_volume=_safe_float(metrics.get("dollar_volume")),
        session_state=_safe_str(snapshot.get("session_state") or metrics.get("session_state")),
        scanner_signal_quality=_safe_float(metrics.get("scan_signal_quality_score")),
        quote_age_seconds=_safe_float(snapshot.get("quote_age_seconds") or metrics.get("quote_age_seconds")),
        price_source_used=_safe_str(snapshot.get("price_source_used") or metrics.get("price_source_used")),
        market_bullish_count=_safe_float(regime_counts.get("bullish")),
        market_bearish_count=_safe_float(regime_counts.get("bearish")),
        holding_duration_bars=_safe_float(exit_state.get("holding_bars") or metrics.get("holding_duration_bars")),
        unrealized_return=_safe_float(exit_state.get("unrealized_return") or metrics.get("unrealized_return")),
        favorable_excursion_r=_safe_float(
            exit_state.get("favorable_excursion_r") or metrics.get("favorable_excursion_r")
        ),
        adverse_excursion_r=_safe_float(metrics.get("adverse_excursion_r")),
        forward_return=_safe_float(metrics.get("forward_return")),
        max_favorable_excursion=_safe_float(metrics.get("max_favorable_excursion")),
        max_adverse_excursion=_safe_float(metrics.get("max_adverse_excursion")),
        realized_return=_safe_float(metrics.get("realized_return")),
        risk_adjusted_return=_safe_float(metrics.get("risk_adjusted_return")),
        news_sentiment_label=_safe_str((news_features or {}).get("sentiment_label")),
        news_sentiment_score=_safe_float((news_features or {}).get("sentiment_score")),
        news_relevance_score=_safe_float((news_features or {}).get("relevance_score")),
        news_risk_tags_count=float(len(risk_tags)),
        news_catalyst_score=_safe_float((news_features or {}).get("catalyst_score")),
        news_source_diversity_count=_safe_float((news_features or {}).get("source_diversity_count")),
        news_cross_source_confirmation=_safe_float(bool((news_features or {}).get("cross_source_confirmation"))),
        news_benzinga_headline_count=_safe_float((news_features or {}).get("benzinga_headline_count")),
        news_sec_event_flag=_safe_float(bool((news_features or {}).get("sec_event_flag"))),
        news_sec_form_type=_safe_str((news_features or {}).get("sec_form_type")),
        outcome_classification=outcome_classification,
        label=label,
        label_source=label_source,
        metadata={
            "feature_version": FEATURE_VERSION,
            "reason": signal.reason,
            "decision_code": metrics.get("decision_code"),
        },
    )


def feature_dict(row: SignalFeatureRow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, SignalFeatureRow):
        return row.to_dict()
    return dict(row)
