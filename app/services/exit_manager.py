from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.models import AssetClass
from app.portfolio.portfolio import Portfolio, Position
from app.strategies.base import Signal, TradeSignal


@dataclass
class ExitEvaluation:
    signal: TradeSignal | None
    state: dict[str, Any] = field(default_factory=dict)


class ExitManager:
    """Generate reduce-only long exits for tracked long positions."""

    tp1_fraction: float = 0.5
    tp2_fraction: float = 0.5

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def evaluate_long_position(
        self,
        symbol: str,
        price: float | None,
        *,
        asset_class: AssetClass = AssetClass.UNKNOWN,
        strategy_name: str | None = None,
    ) -> ExitEvaluation:
        position = self.portfolio.get_position(symbol)
        if position is None or price is None or price <= 0:
            return ExitEvaluation(signal=None, state=self.portfolio.get_position_state(symbol))
        if not self.portfolio.is_sellable_long_position(symbol):
            return ExitEvaluation(signal=None, state=self._build_state(position))

        current_price = float(price)
        position.current_price = current_price
        self.portfolio.record_new_high(symbol, current_price)

        metadata = dict(position.entry_signal_metadata)
        base_stop = self._float_or_none(position.current_stop)
        if base_stop is None:
            base_stop = self._float_or_none(metadata.get("stop_price"))
        if base_stop is not None and position.current_stop != base_stop:
            self.portfolio.update_stop_target_state(symbol, current_stop=base_stop)

        tp2_price = self._float_or_none(metadata.get("target_price"))
        tp1_price = self._derive_tp1_price(position, tp2_price)
        trail_stop = self._derive_trailing_stop(position, base_stop)
        if trail_stop is not None and position.current_stop != trail_stop:
            self.portfolio.update_stop_target_state(symbol, current_stop=trail_stop)

        state = self._build_state(position, tp1_price=tp1_price, tp2_price=tp2_price, trailing_stop=trail_stop)
        current_stop = state.get("current_stop")
        resolved_strategy_name = str(
            strategy_name
            or metadata.get("strategy_name")
            or "exit_manager"
        )
        resolved_asset_class = asset_class if asset_class != AssetClass.UNKNOWN else position.asset_class

        if current_stop is not None and current_price <= current_stop:
            exit_stage = "trail" if position.tp2_hit and trail_stop is not None else "stop"
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    position_size=position.quantity,
                    exit_fraction=1.0,
                    exit_stage=exit_stage,
                    current_stop=current_stop,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    trailing_stop=trail_stop,
                    state=state,
                    reason=(
                        "Trailing stop hit on remaining long position."
                        if exit_stage == "trail"
                        else "Hard stop hit on tracked long position."
                    ),
                ),
                state=state,
            )

        if not position.tp1_hit and tp1_price is not None and current_price >= tp1_price:
            next_stop = max(
                value
                for value in [current_stop, position.entry_price]
                if value is not None
            )
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    position_size=self._partial_exit_quantity(position, self.tp1_fraction),
                    exit_fraction=self.tp1_fraction,
                    exit_stage="tp1",
                    current_stop=current_stop,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    trailing_stop=trail_stop,
                    next_stop=next_stop,
                    state=state,
                    reason="First profit target reached. Reduce half of the remaining long position.",
                ),
                state=state,
            )

        if position.tp1_hit and not position.tp2_hit and tp2_price is not None and current_price >= tp2_price:
            next_stop_candidates = [current_stop, tp1_price, trail_stop, position.entry_price]
            next_stop = max(value for value in next_stop_candidates if value is not None)
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    position_size=self._partial_exit_quantity(position, self.tp2_fraction),
                    exit_fraction=self.tp2_fraction,
                    exit_stage="tp2",
                    current_stop=current_stop,
                    tp1_price=tp1_price,
                    tp2_price=tp2_price,
                    trailing_stop=trail_stop,
                    next_stop=next_stop,
                    state=state,
                    reason="Second profit target reached. Reduce half of the remaining long position.",
                ),
                state=state,
            )

        return ExitEvaluation(signal=None, state=state)

    def _build_state(
        self,
        position: Position,
        *,
        tp1_price: float | None = None,
        tp2_price: float | None = None,
        trailing_stop: float | None = None,
    ) -> dict[str, Any]:
        return {
            **self.portfolio.get_position_state(position.symbol),
            "entry_price": position.entry_price,
            "quantity": position.quantity,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "trailing_stop": trailing_stop,
        }

    def _build_exit_signal(
        self,
        *,
        position: Position,
        asset_class: AssetClass,
        strategy_name: str,
        price: float,
        position_size: float,
        exit_fraction: float,
        exit_stage: str,
        current_stop: float | None,
        tp1_price: float | None,
        tp2_price: float | None,
        trailing_stop: float | None,
        state: dict[str, Any],
        reason: str,
        next_stop: float | None = None,
    ) -> TradeSignal:
        signal = TradeSignal(
            symbol=position.symbol,
            signal=Signal.SELL,
            asset_class=asset_class,
            strategy_name=strategy_name,
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            exit_fraction=exit_fraction,
            exit_stage=exit_stage,
            price=price,
            entry_price=price,
            stop_price=current_stop,
            target_price=tp2_price,
            position_size=min(position.quantity, position_size),
            trailing_stop=trailing_stop,
            reason=reason,
            metrics={
                "decision_code": "exit_signal",
                "exit_state": state,
                "current_stop": current_stop,
                "next_stop": next_stop,
                "tp1_price": tp1_price,
                "tp2_price": tp2_price,
                "trailing_stop": trailing_stop,
            },
        )
        signal.apply_intent_defaults()
        return signal

    def _derive_tp1_price(self, position: Position, tp2_price: float | None) -> float | None:
        if tp2_price is None or tp2_price <= position.entry_price:
            return None
        return position.entry_price + ((tp2_price - position.entry_price) * 0.5)

    def _derive_trailing_stop(self, position: Position, base_stop: float | None) -> float | None:
        if not position.tp2_hit:
            return base_stop

        trail_anchor = self._float_or_none(position.entry_signal_metadata.get("trailing_stop"))
        if trail_anchor is not None and trail_anchor < position.entry_price:
            trail_distance = position.entry_price - trail_anchor
        elif base_stop is not None and base_stop < position.entry_price:
            trail_distance = position.entry_price - base_stop
        else:
            trail_distance = None

        if trail_distance is None:
            return None

        trailing_stop = max(
            position.entry_price,
            (position.highest_price_since_entry or position.entry_price) - trail_distance,
        )
        if base_stop is not None:
            trailing_stop = max(trailing_stop, base_stop)
        return trailing_stop

    def _partial_exit_quantity(self, position: Position, fraction: float) -> float:
        raw_quantity = max(0.0, position.quantity * fraction)
        if position.asset_class == AssetClass.CRYPTO:
            return min(position.quantity, raw_quantity)
        return min(position.quantity, max(1.0, raw_quantity))

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
