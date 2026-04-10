from __future__ import annotations

import pandas as pd

from app.config.settings import Settings
from app.domain.models import AssetClass, AssetMetadata, MarketSessionStatus, SessionState
from app.strategies.base import StrategyContext
from app.strategies.registry import build_strategy_registry


def test_strategy_registry_routes_by_asset_class() -> None:
    registry = build_strategy_registry(Settings(_env_file=None, broker_mode="mock"))

    equity_asset = AssetMetadata(symbol="AAPL", name="Apple", asset_class=AssetClass.EQUITY)
    crypto_asset = AssetMetadata(symbol="BTC/USD", name="Bitcoin", asset_class=AssetClass.CRYPTO)

    equity_names = {strategy.name for strategy in registry.list_for_asset(equity_asset)}
    crypto_names = {strategy.name for strategy in registry.list_for_asset(crypto_asset)}

    assert "equity_momentum_breakout" in equity_names
    assert "equity_trend_pullback" in equity_names
    assert "crypto_momentum_trend" in crypto_names
    assert "equity_momentum_breakout" not in crypto_names


def test_strategy_registry_selects_best_signal_for_crypto() -> None:
    registry = build_strategy_registry(Settings(_env_file=None, broker_mode="mock"))
    asset = AssetMetadata(symbol="BTC/USD", name="Bitcoin", asset_class=AssetClass.CRYPTO)
    data = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=25, freq="D"),
            "Open": [100 + i for i in range(25)],
            "High": [101 + i for i in range(25)],
            "Low": [99 + i for i in range(25)],
            "Close": [100 + i * 1.5 for i in range(25)],
            "Volume": [1000 + i * 20 for i in range(25)],
        }
    )
    context = StrategyContext(
        asset=asset,
        session=MarketSessionStatus(
            asset_class=AssetClass.CRYPTO,
            is_open=True,
            session_state=SessionState.ALWAYS_OPEN,
            extended_hours=False,
            is_24_7=True,
        ),
    )

    signal = registry.select_best_signal(asset, data, context)

    assert signal is not None
    assert signal.asset_class == AssetClass.CRYPTO
