from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FEATURE_VERSION = "v2"


@dataclass
class SignalFeatureRow:
    signal_id: str
    cycle_id: str | None
    generated_at: str | None
    model_purpose: str
    symbol: str
    asset_class: str
    strategy_name: str
    signal: str
    direction: str
    exit_stage: str | None
    confidence: float
    entry: float | None
    stop: float | None
    target: float | None
    atr: float | None
    momentum: float | None
    liquidity: float | None
    spread: float | None
    strategy_score: float | None
    entry_ml_score: float | None
    risk_quality_adjustment: float | None
    reward_risk_ratio: float | None
    stop_distance_atr: float | None
    target_distance_atr: float | None
    breakout_distance_atr: float | None
    relative_volume: float | None
    rolling_volatility: float | None
    volatility_compression: float | None
    higher_timeframe_alignment: float | None
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
    holding_duration_bars: float | None
    unrealized_return: float | None
    favorable_excursion_r: float | None
    adverse_excursion_r: float | None
    forward_return: float | None
    max_favorable_excursion: float | None
    max_adverse_excursion: float | None
    realized_return: float | None
    risk_adjusted_return: float | None
    news_sentiment_label: str | None
    news_sentiment_score: float | None
    news_relevance_score: float | None
    news_risk_tags_count: float | None
    news_catalyst_score: float | None
    news_source_diversity_count: float | None
    news_cross_source_confirmation: float | None
    news_benzinga_headline_count: float | None
    news_sec_event_flag: float | None
    news_sec_form_type: str | None
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
            "model_purpose": self.model_purpose,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "strategy_name": self.strategy_name,
            "signal": self.signal,
            "direction": self.direction,
            "exit_stage": self.exit_stage,
            "confidence": self.confidence,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "atr": self.atr,
            "momentum": self.momentum,
            "liquidity": self.liquidity,
            "spread": self.spread,
            "strategy_score": self.strategy_score,
            "entry_ml_score": self.entry_ml_score,
            "risk_quality_adjustment": self.risk_quality_adjustment,
            "reward_risk_ratio": self.reward_risk_ratio,
            "stop_distance_atr": self.stop_distance_atr,
            "target_distance_atr": self.target_distance_atr,
            "breakout_distance_atr": self.breakout_distance_atr,
            "relative_volume": self.relative_volume,
            "rolling_volatility": self.rolling_volatility,
            "volatility_compression": self.volatility_compression,
            "higher_timeframe_alignment": self.higher_timeframe_alignment,
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
            "holding_duration_bars": self.holding_duration_bars,
            "unrealized_return": self.unrealized_return,
            "favorable_excursion_r": self.favorable_excursion_r,
            "adverse_excursion_r": self.adverse_excursion_r,
            "forward_return": self.forward_return,
            "max_favorable_excursion": self.max_favorable_excursion,
            "max_adverse_excursion": self.max_adverse_excursion,
            "realized_return": self.realized_return,
            "risk_adjusted_return": self.risk_adjusted_return,
            "news_sentiment_label": self.news_sentiment_label,
            "news_sentiment_score": self.news_sentiment_score,
            "news_relevance_score": self.news_relevance_score,
            "news_risk_tags_count": self.news_risk_tags_count,
            "news_catalyst_score": self.news_catalyst_score,
            "news_source_diversity_count": self.news_source_diversity_count,
            "news_cross_source_confirmation": self.news_cross_source_confirmation,
            "news_benzinga_headline_count": self.news_benzinga_headline_count,
            "news_sec_event_flag": self.news_sec_event_flag,
            "news_sec_form_type": self.news_sec_form_type,
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
    purpose: str = "entry"
    model_type: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "score": self.score,
            "threshold": self.threshold,
            "passed": self.passed,
            "purpose": self.purpose,
            "model_type": self.model_type,
            "reason": self.reason,
        }
