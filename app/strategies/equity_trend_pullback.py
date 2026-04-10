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
        latest = df.iloc[-1]

        if pd.isna(latest["ema_fast"]) or pd.isna(latest["sma_slow"]) or pd.isna(latest["atr"]):
            return []

        pullback_distance = (latest["Close"] - latest["ema_fast"]) / latest["ema_fast"]
        trend_up = latest["ema_fast"] > latest["sma_slow"]
        near_pullback = -0.03 <= pullback_distance <= 0.01
        confidence = min(1.0, max(0.0, 0.7 - abs(pullback_distance)))
        if trend_up and near_pullback:
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
                    reason="Uptrend intact with controlled pullback toward fast EMA",
                    regime_state="bullish",
                    timestamp=str(latest.name),
                )
            ]

        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                asset_class=context.asset.asset_class if context else AssetClass.EQUITY,
                strategy_name=self.name,
                reason="No qualified trend pullback setup",
                regime_state="neutral" if trend_up else "bearish",
                timestamp=str(latest.name),
            )
        ]
