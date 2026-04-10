from __future__ import annotations

import pandas as pd

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal


class EMACrossoverStrategy(BaseStrategy):
    name = "ema_crossover"
    supported_asset_classes = {AssetClass.EQUITY, AssetClass.ETF}

    def __init__(
        self,
        short_window: int = 12,
        long_window: int = 26,
        atr_multiplier: float = 1.5,
        short_selling_enabled: bool = False,
    ):
        self.short_window = short_window
        self.long_window = long_window
        self.atr_multiplier = atr_multiplier
        self.short_selling_enabled = short_selling_enabled

    def generate_signals(
        self,
        symbol: str,
        data: pd.DataFrame,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        if data.empty:
            return []

        df = data.copy()
        df["ema_short"] = df["Close"].ewm(span=self.short_window, adjust=False).mean()
        df["ema_long"] = df["Close"].ewm(span=self.long_window, adjust=False).mean()
        df["high_low"] = df["High"] - df["Low"]
        df["high_close"] = (df["High"] - df["Close"].shift()).abs()
        df["low_close"] = (df["Low"] - df["Close"].shift()).abs()
        df["true_range"] = df[["high_low", "high_close", "low_close"]].max(axis=1)
        df["atr"] = df["true_range"].rolling(window=14, min_periods=1).mean()

        signals: list[TradeSignal] = []
        has_sellable_long_position = bool(context and context.metadata.get("has_sellable_long_position"))
        for index, row in df.iterrows():
            if pd.isna(row["ema_short"]) or pd.isna(row["ema_long"]):
                continue

            direction = Signal.HOLD
            reason = "No crossover"
            signal_type = "entry"
            if row["ema_short"] > row["ema_long"]:
                direction = Signal.BUY
                reason = "Bullish EMA crossover"
            elif row["ema_short"] < row["ema_long"]:
                if self.short_selling_enabled:
                    direction = Signal.SELL
                    reason = "Bearish EMA crossover"
                elif has_sellable_long_position:
                    direction = Signal.SELL
                    signal_type = "exit"
                    reason = "Bearish EMA crossover exit"
                else:
                    signal_type = "exit"
                    reason = "Bearish EMA crossover ignored because no tracked long position is available to exit"

            stop_loss = row["Close"] - row["atr"] * self.atr_multiplier
            signals.append(
                TradeSignal(
                    symbol=symbol,
                    signal=direction,
                    asset_class=context.asset.asset_class if context else AssetClass.EQUITY,
                    strategy_name=self.name,
                    signal_type=signal_type,
                    strength=abs(row["ema_short"] - row["ema_long"]),
                    price=float(row["Close"]),
                    reason=f"{reason}. ATR stop {stop_loss:.2f}",
                    timestamp=str(index),
                    atr=float(row["atr"]),
                    stop_price=float(stop_loss),
                )
            )

        if not signals:
            return [TradeSignal(symbol=symbol, signal=Signal.HOLD, reason="Insufficient data")]
        return [signals[-1]]
