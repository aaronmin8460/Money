from __future__ import annotations

from app.domain.models import AssetClass
from app.strategies.base import BaseStrategy, Signal, StrategyContext, TradeSignal


class OptionsLiquidityStrategy(BaseStrategy):
    name = "options_liquidity"
    supported_asset_classes = {AssetClass.OPTION}
    signal_only = True

    def generate_signals(
        self,
        symbol: str,
        data: object,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        return [
            TradeSignal(
                symbol=symbol,
                signal=Signal.HOLD,
                asset_class=AssetClass.OPTION,
                strategy_name=self.name,
                signal_type="scan",
                reason="Options scanning is feature-flagged and paper-only.",
            )
        ]
