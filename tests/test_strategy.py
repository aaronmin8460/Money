import pandas as pd

from app.domain.models import AssetClass, AssetMetadata
from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy
from app.strategies.base import Signal, StrategyContext
from app.strategies.ema_crossover import EMACrossoverStrategy


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


def test_ema_crossover_uses_exit_only_behavior_when_short_selling_disabled() -> None:
    dates = pd.date_range(start="2024-01-01", periods=30, freq="D")
    closes = [130 - i for i in range(30)]
    data = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [10_000 for _ in range(30)],
        }
    )
    strategy = EMACrossoverStrategy(short_selling_enabled=False)
    context = StrategyContext(
        asset=AssetMetadata(symbol="AAPL", name="Apple", asset_class=AssetClass.EQUITY),
        metadata={"has_sellable_long_position": False},
    )

    signals = strategy.generate_signals("AAPL", data, context=context)

    assert signals[-1].signal == Signal.HOLD
    assert signals[-1].signal_type == "exit"
    assert "ignored" in (signals[-1].reason or "").lower()


def test_ema_crossover_keeps_bearish_sell_as_exit_when_long_position_exists() -> None:
    dates = pd.date_range(start="2024-01-01", periods=30, freq="D")
    closes = [130 - i for i in range(30)]
    data = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [10_000 for _ in range(30)],
        }
    )
    strategy = EMACrossoverStrategy(short_selling_enabled=False)
    context = StrategyContext(
        asset=AssetMetadata(symbol="AAPL", name="Apple", asset_class=AssetClass.EQUITY),
        metadata={"has_sellable_long_position": True},
    )

    signals = strategy.generate_signals("AAPL", data, context=context)

    assert signals[-1].signal == Signal.SELL
    assert signals[-1].signal_type == "exit"
