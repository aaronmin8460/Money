from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FEATURE_VERSION = "v1"


@dataclass
class SignalFeatureRow:
    signal_id: str
    cycle_id: str | None
    generated_at: str | None
    symbol: str
    asset_class: str
    strategy_name: str
    signal: str
    direction: str
    confidence: float
    entry: float | None
    stop: float | None
    target: float | None
    atr: float | None
    momentum: float | None
    liquidity: float | None
    spread: float | None
    regime: str | None
    latest_price: float | None
    latest_volume: float | None
    avg_volume: float | None
    dollar_volume: float | None
    session_state: str | None
    scanner_signal_quality: float | None
    quote_age_seconds: float | None
    price_source_used: str | None
    market_bullish_count: float | None
    market_bearish_count: float | None
    news_sentiment_label: str | None
    news_sentiment_score: float | None
    news_relevance_score: float | None
    news_risk_tags_count: float | None
    outcome_classification: str | None = None
    label: int | None = None
    label_source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_version": FEATURE_VERSION,
            "signal_id": self.signal_id,
            "cycle_id": self.cycle_id,
            "generated_at": self.generated_at,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "strategy_name": self.strategy_name,
            "signal": self.signal,
            "direction": self.direction,
            "confidence": self.confidence,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "atr": self.atr,
            "momentum": self.momentum,
            "liquidity": self.liquidity,
            "spread": self.spread,
            "regime": self.regime,
            "latest_price": self.latest_price,
            "latest_volume": self.latest_volume,
            "avg_volume": self.avg_volume,
            "dollar_volume": self.dollar_volume,
            "session_state": self.session_state,
            "scanner_signal_quality": self.scanner_signal_quality,
            "quote_age_seconds": self.quote_age_seconds,
            "price_source_used": self.price_source_used,
            "market_bullish_count": self.market_bullish_count,
            "market_bearish_count": self.market_bearish_count,
            "news_sentiment_label": self.news_sentiment_label,
            "news_sentiment_score": self.news_sentiment_score,
            "news_relevance_score": self.news_relevance_score,
            "news_risk_tags_count": self.news_risk_tags_count,
            "outcome_classification": self.outcome_classification,
            "label": self.label,
            "label_source": self.label_source,
            "metadata": self.metadata,
        }


@dataclass
class ModelScoreResult:
    enabled: bool
    score: float | None
    threshold: float | None
    passed: bool
    model_type: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "score": self.score,
            "threshold": self.threshold,
            "passed": self.passed,
            "model_type": self.model_type,
            "reason": self.reason,
        }
