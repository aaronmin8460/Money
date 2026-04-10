from __future__ import annotations

import pandas as pd

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal


class CryptoMomentumTrendStrategy(BaseStrategy):
    name = "crypto_momentum_trend"
    supported_asset_classes = {AssetClass.CRYPTO}

    def __init__(self, fast_window: int = 8, slow_window: int = 21, volatility_window: int = 14):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.volatility_window = volatility_window

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
        df["ema_slow"] = df["Close"].ewm(span=self.slow_window, adjust=False).mean()
        df["returns"] = df["Close"].pct_change()
        df["volatility"] = df["returns"].rolling(window=self.volatility_window).std()
        df["volume_avg"] = df["Volume"].rolling(window=self.volatility_window).mean()
        latest = df.iloc[-1]

        if pd.isna(latest["ema_fast"]) or pd.isna(latest["ema_slow"]) or pd.isna(latest["volatility"]):
            return []

        bullish = latest["ema_fast"] > latest["ema_slow"] and latest["Close"] > latest["ema_fast"]
        volume_support = pd.notna(latest["volume_avg"]) and latest["Volume"] >= latest["volume_avg"]
        momentum = float((latest["ema_fast"] - latest["ema_slow"]) / latest["ema_slow"])
        volatility = float(latest["volatility"])
        confidence = min(1.0, max(0.0, momentum * 20 + max(0.0, 0.05 - volatility)))
        if bullish and volume_support:
            stop_distance = max(0.015, volatility * 2.5) * latest["Close"]
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.BUY,
                    asset_class=AssetClass.CRYPTO,
                    strategy_name=self.name,
                    strength=confidence,
                    confidence_score=confidence,
                    price=float(latest["Close"]),
                    entry_price=float(latest["Close"]),
                    stop_price=float(latest["Close"] - stop_distance),
                    target_price=float(latest["Close"] + stop_distance * 2.2),
                    momentum_score=momentum,
                    reason="24/7 momentum trend with supportive liquidity",
                    regime_state="bullish",
                    timestamp=str(latest.name),
                )
            ]

        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                asset_class=AssetClass.CRYPTO,
                strategy_name=self.name,
                reason="No qualified crypto momentum trend setup",
                regime_state="neutral" if latest["ema_fast"] >= latest["ema_slow"] else "bearish",
                timestamp=str(latest.name),
            )
        ]
