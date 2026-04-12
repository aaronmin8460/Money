from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.domain.models import AssetClass


def safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AssetSignalProfile:
    breakout_window: int
    ema_window: int
    sma_short_window: int
    sma_long_window: int
    higher_timeframe_window: int
    atr_window: int
    volume_window: int
    volatility_window: int
    min_relative_volume: float
    min_reward_risk: float
    min_atr_pct: float
    max_atr_pct: float
    max_breakout_distance_atr: float
    retest_tolerance_pct: float
    stop_atr_multiple: float
    target_atr_multiple: float
    compression_threshold: float


@dataclass(frozen=True)
class TrendState:
    is_aligned: bool
    score: float
    slope: float
    higher_timeframe_aligned: bool
    reference_support: float | None
    reason: str


@dataclass(frozen=True)
class VolatilityState:
    is_tradeable: bool
    atr: float | None
    atr_pct: float | None
    compression_ratio: float | None
    expansion_score: float
    reason: str


@dataclass(frozen=True)
class LiquidityState:
    is_confirmed: bool
    avg_volume: float | None
    relative_volume: float | None
    dollar_volume: float | None
    score: float
    reason: str


@dataclass(frozen=True)
class BreakoutState:
    is_valid: bool
    breakout_level: float | None
    breakout_distance_atr: float | None
    breakout_strength: float
    retest_valid: bool
    extended_move: bool
    reason: str


@dataclass(frozen=True)
class RiskPlan:
    viable: bool
    stop_price: float | None
    target_price: float | None
    reward_risk_ratio: float | None
    stop_distance_atr: float | None
    target_distance_atr: float | None
    risk_quality_score: float
    reason: str


@dataclass(frozen=True)
class SignalQualityScore:
    total: float
    trend_component: float
    breakout_component: float
    volatility_component: float
    liquidity_component: float
    risk_component: float


def default_signal_profiles() -> dict[AssetClass, AssetSignalProfile]:
    return {
        AssetClass.EQUITY: AssetSignalProfile(
            breakout_window=20,
            ema_window=10,
            sma_short_window=20,
            sma_long_window=50,
            higher_timeframe_window=100,
            atr_window=14,
            volume_window=20,
            volatility_window=20,
            min_relative_volume=1.15,
            min_reward_risk=1.35,
            min_atr_pct=0.005,
            max_atr_pct=0.12,
            max_breakout_distance_atr=0.85,
            retest_tolerance_pct=0.012,
            stop_atr_multiple=1.9,
            target_atr_multiple=3.1,
            compression_threshold=0.95,
        ),
        AssetClass.ETF: AssetSignalProfile(
            breakout_window=18,
            ema_window=10,
            sma_short_window=20,
            sma_long_window=40,
            higher_timeframe_window=80,
            atr_window=14,
            volume_window=20,
            volatility_window=20,
            min_relative_volume=1.05,
            min_reward_risk=1.2,
            min_atr_pct=0.002,
            max_atr_pct=0.06,
            max_breakout_distance_atr=0.65,
            retest_tolerance_pct=0.008,
            stop_atr_multiple=1.6,
            target_atr_multiple=2.6,
            compression_threshold=0.98,
        ),
    }


def add_pipeline_indicators(df: pd.DataFrame, profile: AssetSignalProfile) -> pd.DataFrame:
    enriched = df.copy()
    enriched["ema_fast"] = enriched["Close"].ewm(span=profile.ema_window, adjust=False).mean()
    enriched["sma_short"] = enriched["Close"].rolling(profile.sma_short_window, min_periods=1).mean()
    enriched["sma_long"] = enriched["Close"].rolling(profile.sma_long_window, min_periods=1).mean()
    enriched["sma_higher"] = enriched["Close"].rolling(profile.higher_timeframe_window, min_periods=1).mean()

    enriched["high_low"] = enriched["High"] - enriched["Low"]
    enriched["high_close"] = (enriched["High"] - enriched["Close"].shift()).abs()
    enriched["low_close"] = (enriched["Low"] - enriched["Close"].shift()).abs()
    enriched["true_range"] = enriched[["high_low", "high_close", "low_close"]].max(axis=1)
    enriched["atr"] = enriched["true_range"].rolling(profile.atr_window, min_periods=1).mean()
    enriched["atr_pct"] = enriched["atr"] / enriched["Close"].replace(0, pd.NA)

    enriched["avg_volume"] = enriched["Volume"].rolling(profile.volume_window, min_periods=1).mean()
    enriched["relative_volume"] = enriched["Volume"] / enriched["avg_volume"].replace(0, pd.NA)
    enriched["breakout_level"] = (
        enriched["High"].rolling(profile.breakout_window, min_periods=1).max().shift(1)
    )
    enriched["return_5"] = enriched["Close"].pct_change(5)
    enriched["return_20"] = enriched["Close"].pct_change(20)
    enriched["rolling_vol_short"] = enriched["Close"].pct_change().rolling(10, min_periods=2).std()
    enriched["rolling_vol_long"] = (
        enriched["Close"].pct_change().rolling(profile.volatility_window, min_periods=5).std()
    )
    enriched["compression_ratio"] = enriched["rolling_vol_short"] / enriched["rolling_vol_long"].replace(0, pd.NA)
    return enriched


def resolve_trend_state(df: pd.DataFrame) -> TrendState:
    latest = df.iloc[-1]
    close = safe_float(latest.get("Close")) or 0.0
    ema_fast = safe_float(latest.get("ema_fast"))
    sma_short = safe_float(latest.get("sma_short"))
    sma_long = safe_float(latest.get("sma_long"))
    sma_higher = safe_float(latest.get("sma_higher"))
    slope = 0.0
    if len(df) >= 5:
        reference = safe_float(df["sma_short"].iloc[-5])
        if reference not in {None, 0.0} and sma_short is not None:
            slope = (sma_short - reference) / abs(reference)

    higher_timeframe_aligned = bool(
        sma_higher is not None and close > sma_higher
    )
    is_aligned = bool(
        ema_fast is not None
        and sma_short is not None
        and sma_long is not None
        and close > ema_fast >= sma_short >= sma_long
        and slope >= 0
        and higher_timeframe_aligned
    )
    score = 0.0
    if close > 0:
        if ema_fast is not None and close > ema_fast:
            score += 0.2
        if sma_short is not None and close > sma_short:
            score += 0.2
        if sma_long is not None and sma_short is not None and sma_short >= sma_long:
            score += 0.2
        if higher_timeframe_aligned:
            score += 0.2
        score += max(0.0, min(0.2, slope * 5))
    return TrendState(
        is_aligned=is_aligned,
        score=max(0.0, min(1.0, score)),
        slope=slope,
        higher_timeframe_aligned=higher_timeframe_aligned,
        reference_support=sma_short,
        reason="Trend aligned across short and higher timeframes." if is_aligned else "Trend alignment is weak.",
    )


def resolve_volatility_state(df: pd.DataFrame, profile: AssetSignalProfile) -> VolatilityState:
    latest = df.iloc[-1]
    atr = safe_float(latest.get("atr"))
    atr_pct = safe_float(latest.get("atr_pct"))
    compression_ratio = safe_float(latest.get("compression_ratio"))
    if atr is None or atr_pct is None:
        return VolatilityState(
            is_tradeable=False,
            atr=atr,
            atr_pct=atr_pct,
            compression_ratio=compression_ratio,
            expansion_score=0.0,
            reason="ATR data is unavailable.",
        )

    in_band = profile.min_atr_pct <= atr_pct <= profile.max_atr_pct
    expansion_score = max(0.0, min(1.0, 1.0 - abs((compression_ratio or 1.0) - profile.compression_threshold)))
    return VolatilityState(
        is_tradeable=in_band,
        atr=atr,
        atr_pct=atr_pct,
        compression_ratio=compression_ratio,
        expansion_score=expansion_score,
        reason="Volatility regime is tradeable." if in_band else "Volatility regime is out of bounds.",
    )


def resolve_liquidity_state(df: pd.DataFrame, latest_price: float | None, profile: AssetSignalProfile) -> LiquidityState:
    latest = df.iloc[-1]
    avg_volume = safe_float(latest.get("avg_volume"))
    relative_volume = safe_float(latest.get("relative_volume"))
    dollar_volume = None
    if avg_volume is not None and latest_price is not None:
        dollar_volume = avg_volume * latest_price
    confirmed = bool(relative_volume is not None and relative_volume >= profile.min_relative_volume)
    score = 0.0
    if relative_volume is not None:
        score += max(0.0, min(0.7, relative_volume / max(profile.min_relative_volume, 1e-6) * 0.5))
    if dollar_volume is not None and dollar_volume > 0:
        score += max(0.0, min(0.3, min(1.0, dollar_volume / 10_000_000.0) * 0.3))
    return LiquidityState(
        is_confirmed=confirmed,
        avg_volume=avg_volume,
        relative_volume=relative_volume,
        dollar_volume=dollar_volume,
        score=max(0.0, min(1.0, score)),
        reason="Volume and liquidity confirmed." if confirmed else "Volume confirmation is weak.",
    )


def resolve_breakout_state(df: pd.DataFrame, profile: AssetSignalProfile) -> BreakoutState:
    latest = df.iloc[-1]
    close = safe_float(latest.get("Close")) or 0.0
    low = safe_float(latest.get("Low")) or close
    breakout_level = safe_float(latest.get("breakout_level"))
    atr = safe_float(latest.get("atr"))
    if breakout_level is None or atr in {None, 0.0}:
        return BreakoutState(
            is_valid=False,
            breakout_level=breakout_level,
            breakout_distance_atr=None,
            breakout_strength=0.0,
            retest_valid=False,
            extended_move=False,
            reason="Breakout context is unavailable.",
        )

    breakout_distance_atr = max(0.0, (close - breakout_level) / atr)
    retest_valid = low <= (breakout_level * (1 + profile.retest_tolerance_pct))
    extended_move = breakout_distance_atr > profile.max_breakout_distance_atr
    is_valid = close > breakout_level and not extended_move and (retest_valid or breakout_distance_atr <= 0.35)
    breakout_strength = max(0.0, min(1.0, 1.0 - (breakout_distance_atr / max(profile.max_breakout_distance_atr, 1e-6))))
    return BreakoutState(
        is_valid=is_valid,
        breakout_level=breakout_level,
        breakout_distance_atr=breakout_distance_atr,
        breakout_strength=breakout_strength,
        retest_valid=retest_valid,
        extended_move=extended_move,
        reason=(
            "Breakout trigger confirmed."
            if is_valid
            else ("Move is too extended above breakout." if extended_move else "Breakout trigger not confirmed.")
        ),
    )


def compute_risk_plan(
    *,
    latest_close: float,
    atr: float | None,
    breakout_level: float | None,
    reference_support: float | None,
    profile: AssetSignalProfile,
) -> RiskPlan:
    if atr is None or atr <= 0:
        return RiskPlan(
            viable=False,
            stop_price=None,
            target_price=None,
            reward_risk_ratio=None,
            stop_distance_atr=None,
            target_distance_atr=None,
            risk_quality_score=0.0,
            reason="ATR is unavailable for stop/target planning.",
        )

    fallback_stop = latest_close - (atr * profile.stop_atr_multiple)
    breakout_stop = breakout_level - (atr * 0.35) if breakout_level is not None else fallback_stop
    support_stop = (reference_support or latest_close) - (atr * 0.3)
    stop_price = min(fallback_stop, breakout_stop, support_stop)
    if stop_price >= latest_close:
        stop_price = latest_close - (atr * profile.stop_atr_multiple)

    risk_per_share = latest_close - stop_price
    if risk_per_share <= 0:
        return RiskPlan(
            viable=False,
            stop_price=stop_price,
            target_price=None,
            reward_risk_ratio=None,
            stop_distance_atr=None,
            target_distance_atr=None,
            risk_quality_score=0.0,
            reason="Stop distance is invalid for a long entry.",
        )

    target_price = latest_close + max((atr * profile.target_atr_multiple), (risk_per_share * profile.min_reward_risk))
    reward_risk_ratio = (target_price - latest_close) / max(risk_per_share, 1e-9)
    stop_distance_atr = risk_per_share / atr
    target_distance_atr = (target_price - latest_close) / atr
    viable = reward_risk_ratio >= profile.min_reward_risk
    risk_quality_score = max(0.0, min(1.0, reward_risk_ratio / max(profile.min_reward_risk * 1.5, 1e-6)))
    return RiskPlan(
        viable=viable,
        stop_price=stop_price,
        target_price=target_price,
        reward_risk_ratio=reward_risk_ratio,
        stop_distance_atr=stop_distance_atr,
        target_distance_atr=target_distance_atr,
        risk_quality_score=risk_quality_score,
        reason="Reward/risk is viable." if viable else "Reward/risk is too weak.",
    )


def build_signal_quality_score(
    *,
    trend: TrendState,
    breakout: BreakoutState,
    volatility: VolatilityState,
    liquidity: LiquidityState,
    risk_plan: RiskPlan,
) -> SignalQualityScore:
    trend_component = trend.score * 0.28
    breakout_component = breakout.breakout_strength * 0.24
    volatility_component = volatility.expansion_score * 0.14
    liquidity_component = liquidity.score * 0.16
    risk_component = risk_plan.risk_quality_score * 0.18
    total = trend_component + breakout_component + volatility_component + liquidity_component + risk_component
    return SignalQualityScore(
        total=max(0.0, min(1.0, total)),
        trend_component=trend_component,
        breakout_component=breakout_component,
        volatility_component=volatility_component,
        liquidity_component=liquidity_component,
        risk_component=risk_component,
    )
