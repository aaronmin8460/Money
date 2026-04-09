import pandas as pd

from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy
from app.strategies.base import Signal


def test_regime_momentum_breakout_generates_signal() -> None:
    # Create data with 200+ days for regime
    dates = pd.date_range(start="2024-01-01", periods=250, freq="D")
    # Simulate bullish trend
    closes = [100 + i * 0.1 for i in range(250)]
    data = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [10000 for _ in range(250)],
        }
    )
    strategy = RegimeMomentumBreakoutStrategy()
    signals = strategy.generate_signals("AAPL", data)
    assert signals
    assert signals[-1].signal in {Signal.BUY, Signal.SELL, Signal.HOLD}


def test_regime_filter() -> None:
    dates = pd.date_range(start="2024-01-01", periods=250, freq="D")
    closes = [100 - i * 0.1 for i in range(250)]  # Bearish trend
    data = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [10000 for _ in range(250)],
        }
    )
    strategy = RegimeMomentumBreakoutStrategy()
    signals = strategy.generate_signals("AAPL", data)
    # In a bearish regime, the strategy should not generate a new buy signal.
    assert signals[-1].signal in {Signal.SELL, Signal.HOLD}
    assert "bearish" in signals[-1].regime_state
