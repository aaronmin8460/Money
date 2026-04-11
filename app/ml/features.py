from __future__ import annotations

from typing import Any

from app.ml.schema import FEATURE_VERSION, SignalFeatureRow
from app.monitoring.events import build_signal_id
from app.strategies.base import TradeSignal

CATEGORICAL_FEATURES = [
    "symbol",
    "asset_class",
    "strategy_name",
    "signal",
    "direction",
    "regime",
    "session_state",
    "price_source_used",
    "news_sentiment_label",
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
    "latest_price",
    "latest_volume",
    "avg_volume",
    "dollar_volume",
    "scanner_signal_quality",
    "quote_age_seconds",
    "market_bullish_count",
    "market_bearish_count",
    "news_sentiment_score",
    "news_relevance_score",
    "news_risk_tags_count",
]


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


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
    signal_id = str(metrics.get("signal_id") or build_signal_id(signal.symbol, signal.strategy_name, signal.generated_at))
    risk_tags = news_features.get("risk_tags") if isinstance(news_features, dict) else []
    if not isinstance(risk_tags, list):
        risk_tags = []

    regime_counts = market_overview or {}
    latest = latest_price if latest_price is not None else signal.price or signal.entry_price

    return SignalFeatureRow(
        signal_id=signal_id,
        cycle_id=cycle_id or _safe_str(metrics.get("cycle_id")),
        generated_at=signal.generated_at.isoformat() if signal.generated_at else _safe_str(signal.timestamp),
        symbol=signal.symbol,
        asset_class=signal.asset_class.value,
        strategy_name=signal.strategy_name,
        signal=signal.signal.value,
        direction=signal.direction.value,
        confidence=float(signal.confidence_score or 0.0),
        entry=_safe_float(signal.entry_price),
        stop=_safe_float(signal.stop_price),
        target=_safe_float(signal.target_price),
        atr=_safe_float(signal.atr),
        momentum=_safe_float(signal.momentum_score),
        liquidity=_safe_float(signal.liquidity_score),
        spread=_safe_float(metrics.get("spread_pct") or signal.spread_score),
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
        news_sentiment_label=_safe_str((news_features or {}).get("sentiment_label")),
        news_sentiment_score=_safe_float((news_features or {}).get("sentiment_score")),
        news_relevance_score=_safe_float((news_features or {}).get("relevance_score")),
        news_risk_tags_count=float(len(risk_tags)),
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
