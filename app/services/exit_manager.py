from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config.settings import Settings, get_settings
from app.domain.models import AssetClass
from app.portfolio.portfolio import Portfolio, Position
from app.strategies.base import Signal, TradeSignal


@dataclass
class ExitEvaluation:
    signal: TradeSignal | None
    state: dict[str, Any] = field(default_factory=dict)


class ExitManager:
    """Policy-driven long exit manager with partial targets and hard-risk precedence."""

    def __init__(
        self,
        portfolio: Portfolio,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.settings = settings or get_settings()

    def evaluate_long_position(
        self,
        symbol: str,
        price: float | None,
        *,
        asset_class: AssetClass = AssetClass.UNKNOWN,
        strategy_name: str | None = None,
        current_bar_index: int | None = None,
        regime_state: str | None = None,
        news_features: dict[str, Any] | None = None,
        exit_model_score: float | None = None,
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
        initial_stop = self._float_or_none(metadata.get("stop_price")) or base_stop
        atr = self._float_or_none(metadata.get("atr"))
        if base_stop is None:
            base_stop = initial_stop
        if base_stop is not None and position.current_stop != base_stop:
            self.portfolio.update_stop_target_state(symbol, current_stop=base_stop)

        initial_risk = self._initial_risk(position, initial_stop)
        favorable_excursion_r = self._favorable_excursion_r(position, initial_risk)
        holding_bars = self._holding_bars(metadata, current_bar_index)

        current_stop = base_stop
        break_even_promoted = False
        if initial_risk is not None and self.settings.break_even_after_r_multiple > 0:
            if favorable_excursion_r is not None and favorable_excursion_r >= self.settings.break_even_after_r_multiple:
                promoted_stop = max(position.entry_price, current_stop or position.entry_price)
                if current_stop is None or promoted_stop > current_stop:
                    current_stop = promoted_stop
                    break_even_promoted = True
                    self.portfolio.update_stop_target_state(symbol, current_stop=current_stop)

        trailing_stop = self._derive_trailing_stop(
            position=position,
            base_stop=current_stop,
            initial_risk=initial_risk,
            atr=atr,
            tighten_after_target=bool(position.tp1_hit or metadata.get("hit_target_stages")),
        )
        if trailing_stop is not None and (current_stop is None or trailing_stop > current_stop):
            current_stop = trailing_stop
            self.portfolio.update_stop_target_state(symbol, current_stop=current_stop)

        targets = self._resolve_targets(position, metadata, initial_risk)
        target_prices = {target["stage"]: target["price"] for target in targets if target.get("price") is not None}
        hit_target_stages = set(metadata.get("hit_target_stages") or [])
        unrealized_return = self._unrealized_return(position, current_price)
        news_risk_tags = (news_features or {}).get("risk_tags") if isinstance(news_features, dict) else []
        if not isinstance(news_risk_tags, list):
            news_risk_tags = []

        state = self._build_state(
            position,
            current_stop=current_stop,
            trailing_stop=trailing_stop,
            holding_bars=holding_bars,
            target_prices=target_prices,
            favorable_excursion_r=favorable_excursion_r,
            unrealized_return=unrealized_return,
            break_even_promoted=break_even_promoted,
            regime_state=regime_state,
            exit_model_score=exit_model_score,
        )
        resolved_strategy_name = str(strategy_name or metadata.get("strategy_name") or "exit_manager")
        resolved_asset_class = asset_class if asset_class != AssetClass.UNKNOWN else position.asset_class

        if current_stop is not None and current_price <= current_stop:
            exit_stage = "trail" if trailing_stop is not None and current_stop > position.entry_price else "stop"
            if current_stop >= position.entry_price and exit_stage == "stop":
                exit_stage = "break_even_stop"
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    exit_fraction=1.0,
                    exit_stage=exit_stage,
                    current_stop=current_stop,
                    next_stop=current_stop,
                    target_prices=target_prices,
                    state=state,
                    hit_target_stages=sorted(hit_target_stages),
                    reason="Hard risk exit triggered by the active stop.",
                ),
                state=state,
            )

        emergency_risk_exit = (
            initial_stop is not None
            and current_price <= (initial_stop - max((initial_risk or 0.0) * 0.25, 0.0))
        )
        if emergency_risk_exit or {"halt", "fraud", "bankruptcy"} & set(str(tag).lower() for tag in news_risk_tags):
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    exit_fraction=1.0,
                    exit_stage="emergency",
                    current_stop=current_stop,
                    next_stop=current_stop,
                    target_prices=target_prices,
                    state=state,
                    hit_target_stages=sorted(hit_target_stages),
                    reason="Emergency risk exit triggered by adverse excursion or news risk.",
                ),
                state=state,
            )

        if self.settings.enable_partial_exits:
            for target in targets:
                target_stage = str(target["stage"])
                target_price = self._float_or_none(target.get("price"))
                if target_price is None or target_stage in hit_target_stages:
                    continue
                if current_price < target_price:
                    continue
                next_stop = current_stop
                if target_stage == "tp1":
                    next_stop = max(value for value in [current_stop, position.entry_price] if value is not None)
                elif target_stage.startswith("tp"):
                    next_stop = max(value for value in [current_stop, trailing_stop, position.entry_price] if value is not None)
                updated_hits = sorted(hit_target_stages | {target_stage})
                return ExitEvaluation(
                    signal=self._build_exit_signal(
                        position=position,
                        asset_class=resolved_asset_class,
                        strategy_name=resolved_strategy_name,
                        price=current_price,
                        exit_fraction=float(target["fraction"]),
                        exit_stage=target_stage,
                        current_stop=current_stop,
                        next_stop=next_stop,
                        target_prices=target_prices,
                        state=state,
                        hit_target_stages=updated_hits,
                        reason=f"Profit target {target_stage} reached. Reduce exposure and tighten risk.",
                    ),
                    state=state,
                )

        if self.settings.time_stop_bars > 0 and holding_bars is not None and holding_bars >= self.settings.time_stop_bars:
            exit_fraction = 1.0 if unrealized_return <= 0 else 0.5
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    exit_fraction=exit_fraction,
                    exit_stage="time_stop",
                    current_stop=current_stop,
                    next_stop=max(value for value in [current_stop, position.entry_price] if value is not None),
                    target_prices=target_prices,
                    state=state,
                    hit_target_stages=sorted(hit_target_stages),
                    reason="Time stop triggered because the position became stale.",
                ),
                state=state,
            )

        if regime_state == "bearish":
            exit_fraction = 1.0 if unrealized_return <= 0 else 0.5
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    exit_fraction=exit_fraction,
                    exit_stage="regime_deterioration",
                    current_stop=current_stop,
                    next_stop=max(value for value in [current_stop, position.entry_price] if value is not None),
                    target_prices=target_prices,
                    state=state,
                    hit_target_stages=sorted(hit_target_stages),
                    reason="Regime deterioration exit triggered.",
                ),
                state=state,
            )

        if (
            self.settings.exit_model_enabled
            and exit_model_score is not None
            and exit_model_score >= self.settings.ml_exit_min_score
            and unrealized_return > 0
        ):
            return ExitEvaluation(
                signal=self._build_exit_signal(
                    position=position,
                    asset_class=resolved_asset_class,
                    strategy_name=resolved_strategy_name,
                    price=current_price,
                    exit_fraction=0.25,
                    exit_stage="ml_exit",
                    current_stop=current_stop,
                    next_stop=max(value for value in [current_stop, position.entry_price] if value is not None),
                    target_prices=target_prices,
                    state=state,
                    hit_target_stages=sorted(hit_target_stages),
                    reason="Exit model suggested de-risking a profitable position.",
                    exit_model_score=exit_model_score,
                ),
                state=state,
            )

        return ExitEvaluation(signal=None, state=state)

    def _build_state(
        self,
        position: Position,
        *,
        current_stop: float | None = None,
        trailing_stop: float | None = None,
        holding_bars: int | None = None,
        target_prices: dict[str, float] | None = None,
        favorable_excursion_r: float | None = None,
        unrealized_return: float | None = None,
        break_even_promoted: bool = False,
        regime_state: str | None = None,
        exit_model_score: float | None = None,
    ) -> dict[str, Any]:
        return {
            **self.portfolio.get_position_state(position.symbol),
            "entry_price": position.entry_price,
            "quantity": position.quantity,
            "current_stop": current_stop,
            "trailing_stop": trailing_stop,
            "holding_bars": holding_bars,
            "target_prices": target_prices or {},
            "favorable_excursion_r": favorable_excursion_r,
            "unrealized_return": unrealized_return,
            "break_even_promoted": break_even_promoted,
            "regime_state": regime_state,
            "exit_model_score": exit_model_score,
        }

    def _build_exit_signal(
        self,
        *,
        position: Position,
        asset_class: AssetClass,
        strategy_name: str,
        price: float,
        exit_fraction: float,
        exit_stage: str,
        current_stop: float | None,
        next_stop: float | None,
        target_prices: dict[str, float],
        state: dict[str, Any],
        hit_target_stages: list[str],
        reason: str,
        exit_model_score: float | None = None,
    ) -> TradeSignal:
        requested_quantity = self._partial_exit_quantity(position, exit_fraction)
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
            target_price=target_prices.get("tp2"),
            position_size=min(position.quantity, requested_quantity),
            trailing_stop=state.get("trailing_stop"),
            reason=reason,
            metrics={
                "decision_code": "exit_signal",
                "exit_state": state,
                "current_stop": current_stop,
                "next_stop": next_stop,
                "tp1_price": target_prices.get("tp1"),
                "tp2_price": target_prices.get("tp2"),
                "hit_target_stages": hit_target_stages,
                "exit_model_score": exit_model_score,
            },
        )
        signal.apply_intent_defaults()
        return signal

    def _resolve_targets(
        self,
        position: Position,
        metadata: dict[str, Any],
        initial_risk: float | None,
    ) -> list[dict[str, Any]]:
        if initial_risk is None or initial_risk <= 0:
            return []
        targets: list[dict[str, Any]] = []
        for index, (multiple, fraction) in enumerate(
            zip(
                self.settings.partial_take_profit_levels,
                self.settings.partial_take_profit_fractions,
            ),
            start=1,
        ):
            stage = f"tp{index}"
            targets.append(
                {
                    "stage": stage,
                    "multiple": float(multiple),
                    "fraction": float(fraction),
                    "price": position.entry_price + (initial_risk * float(multiple)),
                }
            )
        return targets

    def _derive_trailing_stop(
        self,
        *,
        position: Position,
        base_stop: float | None,
        initial_risk: float | None,
        atr: float | None,
        tighten_after_target: bool,
    ) -> float | None:
        if self.settings.trailing_stop_mode == "none":
            return base_stop
        highest_price = position.highest_price_since_entry or position.entry_price
        if self.settings.trailing_stop_mode == "atr" and atr is not None and atr > 0:
            atr_multiple = self.settings.trailing_stop_atr_multiple
            if tighten_after_target:
                atr_multiple = max(1.0, atr_multiple * 0.8)
            trailing_stop = highest_price - (atr * atr_multiple)
        elif initial_risk is not None and initial_risk > 0:
            risk_multiple = 0.75 if tighten_after_target else 1.0
            trailing_stop = highest_price - (initial_risk * risk_multiple)
        else:
            return base_stop
        if base_stop is not None:
            trailing_stop = max(trailing_stop, base_stop)
        return trailing_stop

    def _partial_exit_quantity(self, position: Position, fraction: float) -> float:
        raw_quantity = max(0.0, position.quantity * max(0.0, min(1.0, fraction)))
        if fraction >= 1.0:
            return position.quantity
        if position.asset_class == AssetClass.CRYPTO:
            return min(position.quantity, raw_quantity)
        return min(position.quantity, max(1.0, raw_quantity))

    def _initial_risk(self, position: Position, stop_price: float | None) -> float | None:
        if stop_price is None:
            return None
        risk = position.entry_price - stop_price
        return risk if risk > 0 else None

    def _favorable_excursion_r(self, position: Position, initial_risk: float | None) -> float | None:
        if initial_risk is None or initial_risk <= 0:
            return None
        highest_price = position.highest_price_since_entry or position.entry_price
        return max(0.0, (highest_price - position.entry_price) / initial_risk)

    def _holding_bars(self, metadata: dict[str, Any], current_bar_index: int | None) -> int | None:
        entry_bar_index = metadata.get("entry_scan_bar_index")
        if current_bar_index is None or entry_bar_index in {None, ""}:
            return None
        try:
            return max(0, int(current_bar_index) - int(entry_bar_index))
        except (TypeError, ValueError):
            return None

    def _unrealized_return(self, position: Position, current_price: float) -> float:
        if position.entry_price <= 0:
            return 0.0
        return (current_price - position.entry_price) / position.entry_price

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
