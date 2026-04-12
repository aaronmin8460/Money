from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal
from app.strategies.signal_pipeline import (
    AssetSignalProfile,
    BreakoutState,
    LiquidityState,
    RiskPlan,
    SignalQualityScore,
    TrendState,
    VolatilityState,
    add_pipeline_indicators,
    build_signal_quality_score,
    compute_risk_plan,
    default_signal_profiles,
    resolve_breakout_state,
    resolve_liquidity_state,
    resolve_trend_state,
    resolve_volatility_state,
    safe_float,
)


@dataclass(frozen=True)
class PipelineEvaluation:
    passed: bool
    decision_code: str
    reason: str
    trend: TrendState
    volatility: VolatilityState
    liquidity: LiquidityState
    breakout: BreakoutState
    risk_plan: RiskPlan
    quality: SignalQualityScore


class RegimeMomentumBreakoutStrategy(BaseStrategy):
    """Disciplined breakout strategy with modular filters and risk-reward planning."""

    name = "equity_momentum_breakout"
    supported_asset_classes = {AssetClass.EQUITY, AssetClass.ETF}

    def __init__(self) -> None:
        self.regime_symbol = "SPY"
        self.regime_long_sma = 20
        self.regime_short_sma = 10
        self.profiles: dict[AssetClass, AssetSignalProfile] = default_signal_profiles()

    def generate_signals(
        self,
        symbol: str,
        data: Any,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        symbol_df, benchmark_df, regime_df = self._unpack_data(data)
        asset_class = context.asset.asset_class if context else AssetClass.EQUITY
        profile = self._profile_for(asset_class)
        if symbol_df.empty or len(symbol_df) < max(profile.sma_long_window, self.regime_long_sma):
            return [self._build_hold_signal(symbol, asset_class, "insufficient_data", "Insufficient data.")]

        evaluated_df = add_pipeline_indicators(symbol_df, profile)
        latest = evaluated_df.iloc[-1]
        regime_state = self._get_regime_state(evaluated_df, benchmark_df, regime_df)
        has_tracked_long_position = bool((context.metadata if context else {}).get("has_sellable_long_position"))

        if regime_state == "bearish" and has_tracked_long_position:
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.SELL,
                    asset_class=asset_class,
                    strategy_name=self.name,
                    signal_type="exit",
                    order_intent="long_exit",
                    reduce_only=True,
                    exit_stage="regime_deterioration",
                    price=safe_float(latest.get("Close")),
                    reason="Regime deteriorated and a tracked long position is open.",
                    regime_state=regime_state,
                    metrics={
                        "decision_code": "regime_deterioration",
                        "regime_state": regime_state,
                    },
                )
            ]
        if regime_state != "bullish":
            return [
                self._build_hold_signal(
                    symbol,
                    asset_class,
                    "regime_filter",
                    "Broad market regime is not supportive for fresh long breakouts.",
                    regime_state=regime_state,
                )
            ]

        evaluation = self._evaluate_pipeline(evaluated_df, profile)
        if not evaluation.passed:
            return [
                self._build_hold_signal(
                    symbol,
                    asset_class,
                    evaluation.decision_code,
                    evaluation.reason,
                    regime_state=regime_state,
                    metrics=self._pipeline_metrics(evaluation),
                )
            ]

        latest_close = safe_float(latest.get("Close")) or 0.0
        signal = TradeSignal(
            symbol=symbol,
            signal=Signal.BUY,
            asset_class=asset_class,
            strategy_name=self.name,
            strength=evaluation.quality.total,
            confidence_score=evaluation.quality.total,
            price=latest_close,
            entry_price=latest_close,
            reason="Trend, liquidity, breakout trigger, and reward/risk all aligned.",
            atr=evaluation.volatility.atr,
            stop_price=evaluation.risk_plan.stop_price,
            target_price=evaluation.risk_plan.target_price,
            trailing_stop=self._initial_trailing_stop(latest_close, evaluation.risk_plan),
            momentum_score=evaluation.quality.breakout_component + evaluation.quality.trend_component,
            liquidity_score=evaluation.liquidity.score,
            regime_state=regime_state,
            timestamp=str(latest.name),
            metrics={
                **self._pipeline_metrics(evaluation),
                "decision_code": "signal",
                "strategy_score": evaluation.quality.total,
                "breakout_level": evaluation.breakout.breakout_level,
                "breakout_distance_atr": evaluation.breakout.breakout_distance_atr,
                "extended_move": evaluation.breakout.extended_move,
                "reward_risk_ratio": evaluation.risk_plan.reward_risk_ratio,
                "stop_distance_atr": evaluation.risk_plan.stop_distance_atr,
                "target_distance_atr": evaluation.risk_plan.target_distance_atr,
                "relative_volume": evaluation.liquidity.relative_volume,
                "volatility_compression_ratio": evaluation.volatility.compression_ratio,
            },
        )
        return [signal]

    def _profile_for(self, asset_class: AssetClass) -> AssetSignalProfile:
        return self.profiles.get(asset_class, self.profiles[AssetClass.EQUITY])

    def _unpack_data(self, data: Any) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame | None]:
        if isinstance(data, dict):
            return data.get("symbol", pd.DataFrame()), data.get("benchmark"), data.get("regime")
        return data, None, None

    def _get_regime_state(
        self,
        symbol_df: pd.DataFrame,
        benchmark_df: pd.DataFrame | None,
        regime_df: pd.DataFrame | None,
    ) -> str:
        if benchmark_df is not None and not benchmark_df.empty:
            candidate = benchmark_df.copy()
        elif regime_df is not None and not regime_df.empty:
            candidate = regime_df.copy()
        else:
            candidate = symbol_df.copy()
        if len(candidate) < self.regime_long_sma:
            return "unknown"
        candidate["sma_short"] = candidate["Close"].rolling(self.regime_short_sma, min_periods=1).mean()
        candidate["sma_long"] = candidate["Close"].rolling(self.regime_long_sma, min_periods=1).mean()
        latest = candidate.iloc[-1]
        sma_short = safe_float(latest.get("sma_short"))
        sma_long = safe_float(latest.get("sma_long"))
        close = safe_float(latest.get("Close")) or 0.0
        if sma_short is None or sma_long is None:
            return "unknown"
        if close > sma_long and sma_short >= sma_long:
            return "bullish"
        return "bearish"

    def _evaluate_pipeline(self, df: pd.DataFrame, profile: AssetSignalProfile) -> PipelineEvaluation:
        latest = df.iloc[-1]
        latest_close = safe_float(latest.get("Close")) or 0.0
        trend = resolve_trend_state(df)
        volatility = resolve_volatility_state(df, profile)
        liquidity = resolve_liquidity_state(df, latest_close, profile)
        breakout = resolve_breakout_state(df, profile)
        risk_plan = compute_risk_plan(
            latest_close=latest_close,
            atr=volatility.atr,
            breakout_level=breakout.breakout_level,
            reference_support=trend.reference_support,
            profile=profile,
        )
        quality = build_signal_quality_score(
            trend=trend,
            breakout=breakout,
            volatility=volatility,
            liquidity=liquidity,
            risk_plan=risk_plan,
        )

        if not trend.is_aligned:
            return PipelineEvaluation(False, "weak_trend", trend.reason, trend, volatility, liquidity, breakout, risk_plan, quality)
        if not volatility.is_tradeable:
            return PipelineEvaluation(
                False,
                "volatility_filter",
                volatility.reason,
                trend,
                volatility,
                liquidity,
                breakout,
                risk_plan,
                quality,
            )
        if not liquidity.is_confirmed:
            return PipelineEvaluation(
                False,
                "volume_confirmation",
                liquidity.reason,
                trend,
                volatility,
                liquidity,
                breakout,
                risk_plan,
                quality,
            )
        if not breakout.is_valid:
            return PipelineEvaluation(
                False,
                "breakout_trigger",
                breakout.reason,
                trend,
                volatility,
                liquidity,
                breakout,
                risk_plan,
                quality,
            )
        if not risk_plan.viable:
            return PipelineEvaluation(
                False,
                "reward_risk",
                risk_plan.reason,
                trend,
                volatility,
                liquidity,
                breakout,
                risk_plan,
                quality,
            )
        return PipelineEvaluation(
            True,
            "signal",
            "Pipeline passed.",
            trend,
            volatility,
            liquidity,
            breakout,
            risk_plan,
            quality,
        )

    def _pipeline_metrics(self, evaluation: PipelineEvaluation) -> dict[str, Any]:
        return {
            "pipeline": {
                "trend": asdict(evaluation.trend),
                "volatility": asdict(evaluation.volatility),
                "liquidity": asdict(evaluation.liquidity),
                "breakout": asdict(evaluation.breakout),
                "risk_plan": asdict(evaluation.risk_plan),
                "quality": asdict(evaluation.quality),
            }
        }

    def _initial_trailing_stop(self, entry_price: float, risk_plan: RiskPlan) -> float | None:
        if risk_plan.stop_price is None:
            return None
        risk_per_share = entry_price - risk_plan.stop_price
        if risk_per_share <= 0:
            return None
        return entry_price - (risk_per_share * 1.2)

    def _build_hold_signal(
        self,
        symbol: str,
        asset_class: AssetClass,
        decision_code: str,
        reason: str,
        *,
        regime_state: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> TradeSignal:
        return TradeSignal(
            symbol=symbol,
            signal=Signal.HOLD,
            asset_class=asset_class,
            strategy_name=self.name,
            reason=reason,
            regime_state=regime_state,
            metrics={"decision_code": decision_code, **(metrics or {})},
        )
