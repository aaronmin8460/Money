from __future__ import annotations

import pandas as pd

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal


class EquityTrendPullbackStrategy(BaseStrategy):
    name = "equity_trend_pullback"
    supported_asset_classes = {AssetClass.EQUITY, AssetClass.ETF}

    def __init__(self, fast_window: int = 10, slow_window: int = 20, atr_window: int = 14):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.atr_window = atr_window

    def generate_signals(
        self,
        symbol: str,
        data: pd.DataFrame,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        if data.empty or len(data) < self.slow_window:
            return []

        df = data.copy()
        df["ema_fast"] = df["Close"].ewm(span=self.fast_window, adjust=False).mean()
        df["sma_slow"] = df["Close"].rolling(window=self.slow_window).mean()
        df["prev_close"] = df["Close"].shift(1)
        df["high_low"] = df["High"] - df["Low"]
        df["high_close"] = (df["High"] - df["prev_close"]).abs()
        df["low_close"] = (df["Low"] - df["prev_close"]).abs()
        df["true_range"] = df[["high_low", "high_close", "low_close"]].max(axis=1)
        df["atr"] = df["true_range"].rolling(window=self.atr_window, min_periods=1).mean()
        df["avg_volume"] = df["Volume"].rolling(window=20, min_periods=1).mean()
        df["relative_volume"] = df["Volume"] / df["avg_volume"].replace(0, pd.NA)
        latest = df.iloc[-1]

        if pd.isna(latest["ema_fast"]) or pd.isna(latest["sma_slow"]) or pd.isna(latest["atr"]):
            return []

        profile_name = str((context.metadata if context else {}).get("trading_profile") or "conservative").lower()
        pullback_distance = (latest["Close"] - latest["ema_fast"]) / latest["ema_fast"]
        trend_up = latest["ema_fast"] > latest["sma_slow"]
        max_pullback = 0.01
        min_pullback = -0.03
        minimum_relative_volume = 0.95
        if profile_name == "balanced":
            min_pullback = -0.035
            max_pullback = 0.015
            minimum_relative_volume = 0.9
        elif profile_name == "aggressive":
            min_pullback = -0.04
            max_pullback = 0.02
            minimum_relative_volume = 0.85
        near_pullback = min_pullback <= pullback_distance <= max_pullback
        relative_volume = float(latest["relative_volume"]) if not pd.isna(latest["relative_volume"]) else 0.0
        confidence = min(1.0, max(0.0, 0.72 - abs(pullback_distance) + min(0.15, max(0.0, relative_volume - 1.0) * 0.1)))
        if trend_up and near_pullback and relative_volume >= minimum_relative_volume:
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.BUY,
                    asset_class=context.asset.asset_class if context else AssetClass.EQUITY,
                    strategy_name=self.name,
                    strength=confidence,
                    confidence_score=confidence,
                    price=float(latest["Close"]),
                    entry_price=float(latest["Close"]),
                    stop_price=float(latest["Close"] - 1.8 * latest["atr"]),
                    target_price=float(latest["Close"] + 2.5 * latest["atr"]),
                    atr=float(latest["atr"]),
                    momentum_score=float((latest["ema_fast"] - latest["sma_slow"]) / latest["sma_slow"]),
                    liquidity_score=min(1.0, relative_volume / max(minimum_relative_volume, 1e-6) * 0.5),
                    reason="Uptrend intact with controlled pullback/reclaim toward fast EMA",
                    regime_state="bullish",
                    timestamp=str(latest.name),
                    metrics={
                        "strategy_score": confidence,
                        "relative_volume": relative_volume,
                        "profile_name": profile_name,
                    },
                )
            ]

        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                asset_class=context.asset.asset_class if context else AssetClass.EQUITY,
                strategy_name=self.name,
                reason="No qualified trend pullback or reclaim setup",
                regime_state="neutral" if trend_up else "bearish",
                timestamp=str(latest.name),
                metrics={"decision_code": "pullback_setup_not_ready", "profile_name": profile_name},
            )
        ]
