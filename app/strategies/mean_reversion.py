from __future__ import annotations

import pandas as pd

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal


class MeanReversionScannerStrategy(BaseStrategy):
    name = "mean_reversion_scanner"
    supported_asset_classes = {AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO}
    signal_only = True

    def __init__(self, lookback: int = 14):
        self.lookback = lookback

    def generate_signals(
        self,
        symbol: str,
        data: pd.DataFrame,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        if data.empty or len(data) < self.lookback:
            return []

        df = data.copy()
        rolling_mean = df["Close"].rolling(window=self.lookback).mean()
        rolling_std = df["Close"].rolling(window=self.lookback).std()
        latest = df.iloc[-1]
        mean = rolling_mean.iloc[-1]
        std = rolling_std.iloc[-1]
        if pd.isna(mean) or pd.isna(std) or std == 0:
            return []

        z_score = float((latest["Close"] - mean) / std)
        signal = Signal.HOLD
        reason = "No mean reversion extreme"
        if z_score <= -1.75:
            signal = Signal.BUY
            reason = "Price is materially below rolling mean"
        elif z_score >= 1.75:
            signal = Signal.SELL
            reason = "Price is materially above rolling mean"

        return [
            TradeSignal(
                symbol=symbol,
                signal=signal,
                asset_class=context.asset.asset_class if context else AssetClass.EQUITY,
                strategy_name=self.name,
                signal_type="scan",
                strength=min(1.0, abs(z_score) / 3),
                confidence_score=min(1.0, abs(z_score) / 3),
                price=float(latest["Close"]),
                reason=reason,
                regime_state="mean_reversion",
                metrics={"z_score": z_score},
                timestamp=str(latest.name),
            )
        ]
