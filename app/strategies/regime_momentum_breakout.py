from __future__ import annotations

from typing import Any

import pandas as pd

from app.strategies.base import BaseStrategy, Signal, TradeSignal


class RegimeMomentumBreakoutStrategy(BaseStrategy):
    """Advanced strategy using regime filter, momentum ranking, and breakout entries."""

    def __init__(self):
        self.regime_symbol = "SPY"
        self.regime_long_sma = 200
        self.regime_short_sma = 50
        self.ema_window = 20
        self.sma_short = 50
        self.sma_long = 100
        self.atr_window = 14
        self.breakout_window = 20
        self.volume_window = 20
        self.return_window = 20
        self.return_3m_window = 60

    def generate_signals(self, symbol: str, data: Any) -> list[TradeSignal]:
        symbol_df, benchmark_df = self._unpack_data(data)
        if symbol_df.empty or len(symbol_df) < self.regime_long_sma:
            return [TradeSignal(symbol=symbol, signal=Signal.HOLD, reason="Insufficient data")]

        symbol_df = symbol_df.copy()
        symbol_df = self._add_indicators(symbol_df)
        regime_state = self._get_regime_state(symbol_df, benchmark_df)

        if regime_state == "bearish":
            return self._generate_exit_signals(symbol, symbol_df, regime_state)
        if regime_state == "unknown":
            return [TradeSignal(symbol=symbol, signal=Signal.HOLD, reason="Regime unknown", regime_state=regime_state)]

        signals: list[TradeSignal] = []
        for _, row in symbol_df.iterrows():
            candidate = self._evaluate_entry(symbol, row, regime_state)
            if candidate:
                signals.append(candidate)

        if signals:
            return [signals[-1]]

        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                reason="No valid entry signal",
                regime_state=regime_state,
            )
        ]

    def _unpack_data(self, data: Any) -> tuple[pd.DataFrame, pd.DataFrame | None]:
        if isinstance(data, dict):
            return data.get("symbol", pd.DataFrame()), data.get("benchmark")
        return data, None

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df["ema"] = df["Close"].ewm(span=self.ema_window, adjust=False).mean()
        df["sma_short"] = df["Close"].rolling(window=self.sma_short).mean()
        df["sma_long"] = df["Close"].rolling(window=self.sma_long).mean()

        df["high_low"] = df["High"] - df["Low"]
        df["high_close"] = (df["High"] - df["Close"].shift()).abs()
        df["low_close"] = (df["Low"] - df["Close"].shift()).abs()
        df["true_range"] = df[["high_low", "high_close", "low_close"]].max(axis=1)
        df["atr"] = df["true_range"].rolling(window=self.atr_window).mean()

        df["prev_breakout_high"] = df["High"].rolling(window=self.breakout_window).max().shift(1)
        df["avg_volume"] = df["Volume"].rolling(window=self.volume_window).mean()

        df["return_1m"] = df["Close"].pct_change(periods=self.return_window)
        df["return_3m"] = df["Close"].pct_change(periods=self.return_3m_window)

        return df

    def _get_regime_state(self, symbol_df: pd.DataFrame, benchmark_df: pd.DataFrame | None) -> str:
        if benchmark_df is not None and len(benchmark_df) >= self.regime_long_sma:
            benchmark = benchmark_df.copy()
            benchmark["sma_short"] = benchmark["Close"].rolling(window=self.regime_short_sma).mean()
            benchmark["sma_long"] = benchmark["Close"].rolling(window=self.regime_long_sma).mean()
            latest = benchmark.iloc[-1]
            if (
                pd.notna(latest["sma_long"])
                and pd.notna(latest["sma_short"])
                and latest["Close"] > latest["sma_long"]
                and latest["sma_short"] > latest["sma_long"]
            ):
                return "bullish"
            return "bearish"

        latest_symbol = symbol_df.iloc[-1]
        if pd.isna(latest_symbol["sma_long"]) or pd.isna(latest_symbol["sma_short"]):
            return "unknown"
        if (
            latest_symbol["Close"] > latest_symbol["sma_long"]
            and latest_symbol["sma_short"] > latest_symbol["sma_long"]
        ):
            return "bullish"
        return "bearish"

    def _evaluate_entry(self, symbol: str, row: pd.Series, regime_state: str) -> TradeSignal | None:
        if pd.isna(row["ema"]) or pd.isna(row["sma_short"]) or pd.isna(row["sma_long"]) or pd.isna(row["atr"]):
            return None

        close_above_ema = row["Close"] > row["ema"]
        close_above_sma_short = row["Close"] > row["sma_short"]
        sma_short_above_long = row["sma_short"] > row["sma_long"]
        breakout = pd.notna(row["prev_breakout_high"]) and row["Close"] > row["prev_breakout_high"]
        volume_ok = pd.notna(row["avg_volume"]) and row["Volume"] >= 1.2 * row["avg_volume"]

        if close_above_ema and close_above_sma_short and sma_short_above_long and breakout and volume_ok:
            momentum_score = 0.0
            if pd.notna(row["return_1m"]):
                momentum_score += 0.5 * row["return_1m"]
            if pd.notna(row["return_3m"]):
                momentum_score += 0.3 * row["return_3m"]
            distance_above_sma = (
                row["Close"] - row["sma_short"]
            ) / row["sma_short"] if row["sma_short"] > 0 else 0.0
            momentum_score += 0.2 * distance_above_sma

            initial_stop = float(row["Close"] - 2.0 * row["atr"])
            trailing_stop = float(row["Close"] - 2.5 * row["atr"])

            return TradeSignal(
                symbol=symbol,
                signal=Signal.BUY,
                strength=momentum_score,
                price=float(row["Close"]),
                reason="Regime bullish, breakout above 20-day high with volume",
                atr=float(row["atr"]),
                stop_price=initial_stop,
                trailing_stop=trailing_stop,
                momentum_score=momentum_score,
                regime_state=regime_state,
                timestamp=str(row.name),
            )
        return None

    def _generate_exit_signals(self, symbol: str, df: pd.DataFrame, regime_state: str) -> list[TradeSignal]:
        latest = df.iloc[-1]
        if pd.notna(latest["ema"]) and latest["Close"] < latest["ema"]:
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.SELL,
                    price=float(latest["Close"]),
                    reason="Bearish regime and close below EMA",
                    regime_state=regime_state,
                )
            ]
        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                reason="Bearish regime, no exit signal",
                regime_state=regime_state,
            )
        ]
