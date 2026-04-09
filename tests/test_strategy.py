import pandas as pd

from app.strategies.ema_crossover import EMACrossoverStrategy
from app.strategies.base import Signal


def test_ema_crossover_generates_signal() -> None:
    data = pd.DataFrame(
        {
            "Date": pd.date_range(start="2024-01-01", periods=30, freq="D"),
            "Open": [100 + i * 0.2 for i in range(30)],
            "High": [101 + i * 0.2 for i in range(30)],
            "Low": [99 + i * 0.2 for i in range(30)],
            "Close": [100 + i * 0.2 for i in range(30)],
            "Volume": [1000 for _ in range(30)],
        }
    )
    strategy = EMACrossoverStrategy()
    signals = strategy.generate_signals("AAPL", data)
    assert signals
    assert signals[-1].signal in {Signal.BUY, Signal.SELL, Signal.HOLD}
