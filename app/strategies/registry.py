from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from app.config.settings import Settings, get_settings
from app.domain.models import AssetMetadata
from app.strategies.base import StrategyContext, TradeSignal
from app.strategies.crypto_momentum_trend import CryptoMomentumTrendStrategy
from app.strategies.ema_crossover import EMACrossoverStrategy
from app.strategies.equity_trend_pullback import EquityTrendPullbackStrategy
from app.strategies.mean_reversion import MeanReversionScannerStrategy
from app.strategies.options_liquidity import OptionsLiquidityStrategy
from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy

StrategyFactory = Callable[[], object]

STRATEGY_NAME_ALIASES = {
    "regime_momentum_breakout": "equity_momentum_breakout",
}


@dataclass
class StrategyRegistry:
    settings: Settings

    def __post_init__(self) -> None:
        self._strategy_factories: dict[str, StrategyFactory] = {
            "equity_momentum_breakout": RegimeMomentumBreakoutStrategy,
            "equity_trend_pullback": EquityTrendPullbackStrategy,
            "crypto_momentum_trend": CryptoMomentumTrendStrategy,
            "mean_reversion_scanner": MeanReversionScannerStrategy,
            "ema_crossover": lambda: EMACrossoverStrategy(
                short_selling_enabled=self.settings.short_selling_enabled
            ),
        }
        if self.settings.option_trading_enabled:
            self._strategy_factories["options_liquidity"] = OptionsLiquidityStrategy
        self._strategies = {
            name: factory()
            for name, factory in self._strategy_factories.items()
        }

    def resolve_name(self, name: str | None) -> str:
        candidate = (name or "").strip().lower()
        return STRATEGY_NAME_ALIASES.get(candidate, candidate)

    def get(self, name: str | None) -> object:
        resolved_name = self.resolve_name(name)
        strategy = self._strategies.get(resolved_name)
        if strategy is None:
            available = ", ".join(sorted(self._strategies))
            raise ValueError(f"Unknown strategy '{name}'. Available strategies: {available}")
        return strategy

    def list_available(self) -> list[object]:
        return [self._strategies[name] for name in sorted(self._strategies)]

    def list_for_asset(self, asset: AssetMetadata) -> list[object]:
        enabled_switches = {
            name.lower(): enabled
            for name, enabled in self.settings.strategy_switches.items()
        }
        return [
            strategy
            for strategy in self.list_available()
            if strategy.supports(asset.asset_class)
            and enabled_switches.get(strategy.name.lower(), True)
        ]

    def generate_signals(
        self,
        asset: AssetMetadata,
        data: pd.DataFrame,
        context: StrategyContext,
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for strategy in self.list_for_asset(asset):
            strategy_input = data
            if strategy.name == "equity_momentum_breakout" and context.metadata.get("benchmark_bars") is not None:
                strategy_input = {"symbol": data, "benchmark": context.metadata["benchmark_bars"]}
            produced = strategy.generate_signals(asset.symbol, strategy_input, context=context)
            if produced:
                signals.extend(produced)
        return signals

    def select_best_signal(
        self,
        asset: AssetMetadata,
        data: pd.DataFrame,
        context: StrategyContext,
    ) -> TradeSignal | None:
        signals = self.generate_signals(asset, data, context=context)
        if not signals:
            return None
        return sorted(
            signals,
            key=lambda signal: (
                signal.signal.value != "HOLD",
                signal.confidence_score or 0.0,
                signal.momentum_score or 0.0,
            ),
            reverse=True,
        )[0]


def build_strategy_registry(settings: Settings | None = None) -> StrategyRegistry:
    return StrategyRegistry(settings=settings or get_settings())
