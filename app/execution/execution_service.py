from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.config.settings import Settings, get_settings
from app.db.models import (
    FillRecord,
    NormalizedSignalRecord,
    Order as LegacyOrderRecord,
    PositionSnapshotRecord,
    SignalEvent,
)
from app.db.session import SessionLocal
from app.domain.models import AssetClass
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskDecision, RiskManager
from app.services.broker import BrokerInterface, OrderRequest
from app.services.market_data import MarketDataService, infer_asset_class
from app.services.tranche_state import TranchePlanState, TrancheStateStore
from app.strategies.base import BaseStrategy, Signal, TradeSignal


logger = get_logger("execution")

MONEY_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.0001")
FRACTIONAL_QTY_QUANTUM = Decimal("0.000001")
WHOLE_QTY_QUANTUM = Decimal("1")


@dataclass
class OrderSizing:
    quantity: float | None
    notional: float | None
    price: float
    metadata: dict[str, Any]


@dataclass
class ScaleInDecision:
    approved: bool
    reason: str
    rule: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionService:
    broker: BrokerInterface
    portfolio: Portfolio
    risk_manager: RiskManager
    dry_run: bool = True
    market_data_service: MarketDataService | None = None
    settings: Settings = field(default_factory=get_settings)
    tranche_state: TrancheStateStore = field(default_factory=TrancheStateStore)

    def process_signal(self, signal: TradeSignal) -> dict[str, Any]:
        self._annotate_signal_context(signal)
        if signal.signal == Signal.HOLD:
            normalized_snapshot = (signal.metrics or {}).get("normalized_snapshot", {})
            latest_price = signal.price or normalized_snapshot.get("evaluation_price")
            hold_risk = RiskDecision(
                approved=False,
                reason=signal.reason or "No trade signal.",
                rule=(signal.metrics or {}).get("decision_code", "no_signal"),
                details={
                    **(signal.metrics or {}),
                    "latest_price": latest_price,
                },
            )
            hold_proposal = OrderRequest(
                symbol=signal.symbol,
                side=Signal.HOLD.value,
                quantity=0.0,
                notional=None,
                asset_class=signal.asset_class,
                price=latest_price,
                time_in_force="gtc" if signal.asset_class == AssetClass.CRYPTO else "day",
                is_dry_run=True,
                metadata={"hold": True, "normalized_snapshot": normalized_snapshot},
            )
            self._persist_signal_event(signal, latest_price or 0.0, 0.0, hold_risk)
            self._notify_trade_event(
                action="hold",
                signal=signal,
                proposal=hold_proposal,
                risk_decision=hold_risk,
            )
            logger.info("Signal is HOLD", extra={"symbol": signal.symbol, "strategy": signal.strategy_name})
            return {
                "symbol": signal.symbol,
                "signal": signal.signal.value,
                "latest_price": latest_price,
                "proposal": {},
                "risk": hold_risk.to_dict(),
                "action": "hold",
                "order": None,
            }

        price = signal.entry_price or signal.price or self._latest_price(signal)
        asset_class = signal.asset_class if signal.asset_class != AssetClass.UNKNOWN else infer_asset_class(signal.symbol)
        scale_in_decision = self.build_scale_in_decision(signal=signal, asset_class=asset_class, price=price)
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics["scale_in"] = scale_in_decision.details

        if not scale_in_decision.approved:
            rejected_proposal = OrderRequest(
                symbol=signal.symbol,
                side=signal.signal.value,
                quantity=0.0,
                notional=None,
                asset_class=asset_class,
                price=price,
                time_in_force="gtc" if asset_class == AssetClass.CRYPTO else "day",
                is_dry_run=self.dry_run or not self.settings.trading_enabled,
                metadata={"scale_in": scale_in_decision.details, "sizing": scale_in_decision.details},
            )
            risk_decision = RiskDecision(
                approved=False,
                reason=scale_in_decision.reason,
                rule=scale_in_decision.rule,
                details=scale_in_decision.details,
            )
            self.risk_manager.record_manual_rejection(signal.symbol, signal.signal.value, risk_decision)
            self._persist_signal_event(signal, price, 0.0, risk_decision)
            self._notify_trade_event(
                action="rejected",
                signal=signal,
                proposal=rejected_proposal,
                risk_decision=risk_decision,
            )
            return {
                "symbol": signal.symbol,
                "signal": signal.signal.value,
                "latest_price": price,
                "proposal": {
                    "symbol": rejected_proposal.symbol,
                    "asset_class": asset_class.value,
                    "side": rejected_proposal.side,
                    "quantity": rejected_proposal.quantity,
                    "notional": rejected_proposal.notional,
                    "price": rejected_proposal.price,
                    "time_in_force": rejected_proposal.time_in_force,
                    "is_dry_run": rejected_proposal.is_dry_run,
                },
                "risk": risk_decision.to_dict(),
                "action": "rejected",
                "order": None,
            }

        proposal = self._build_order_request(signal, asset_class, price)
        risk_decision = self._evaluate_signal_risk(signal, proposal, price)
        self._persist_signal_event(signal, price, proposal.quantity or 0.0, risk_decision)

        proposal_payload = {
            "symbol": proposal.symbol,
            "asset_class": asset_class.value,
            "side": proposal.side.value if hasattr(proposal.side, "value") else str(proposal.side),
            "quantity": proposal.quantity,
            "notional": proposal.notional,
            "price": proposal.price,
            "time_in_force": proposal.time_in_force,
            "is_dry_run": proposal.is_dry_run,
        }

        if not risk_decision.approved:
            logger.warning("Order blocked by risk manager", extra={"reason": risk_decision.reason, "symbol": proposal.symbol})
            if signal.signal == Signal.BUY:
                self.tranche_state.mark_decision(
                    signal.symbol,
                    reason="Tranche blocked by risk manager.",
                    blocked_reason=f"{risk_decision.rule}: {risk_decision.reason}",
                )
            self._notify_trade_event(
                action="rejected",
                signal=signal,
                proposal=proposal,
                risk_decision=risk_decision,
            )
            return {
                "symbol": proposal.symbol,
                "signal": signal.signal.value,
                "latest_price": price,
                "proposal": proposal_payload,
                "risk": risk_decision.to_dict(),
                "action": "rejected",
                "order": None,
            }

        self.portfolio.mark_to_market({proposal.symbol: price})
        executed_order = self.broker.submit_order(proposal)
        if not proposal.is_dry_run:
            self.portfolio.update_position(
                proposal.symbol,
                str(signal.signal.value),
                proposal.quantity or 0.0,
                price,
                asset_class=asset_class,
                exchange=signal.metrics.get("exchange") if signal.metrics else None,
            )
            self.risk_manager.mark_executed(proposal.symbol, signal.strategy_name)
            self._record_post_execution_state(signal, proposal, price, asset_class)
        self._persist_order(signal, proposal, executed_order)

        action = "dry_run" if proposal.is_dry_run else "submitted"
        logger.info("Order processed", extra={"action": action, "order": executed_order})
        self._notify_trade_event(
            action=action,
            signal=signal,
            proposal=proposal,
            risk_decision=risk_decision,
            order=executed_order,
        )
        return {
            "symbol": proposal.symbol,
            "signal": signal.signal.value,
            "latest_price": price,
            "proposal": proposal_payload,
            "risk": risk_decision.to_dict(),
            "action": action,
            "order": executed_order,
        }

    def _build_order_request(self, signal: TradeSignal, asset_class: AssetClass, price: float) -> OrderRequest:
        sizing = self._calculate_position_size(signal, asset_class, price)
        tif = "gtc" if asset_class == AssetClass.CRYPTO else "day"
        return OrderRequest(
            symbol=signal.symbol,
            side=signal.signal.value,
            quantity=sizing.quantity,
            notional=sizing.notional,
            asset_class=asset_class,
            price=sizing.price,
            time_in_force=tif,
            is_dry_run=self.dry_run or not self.settings.trading_enabled,
            metadata=sizing.metadata,
        )

    def _record_post_execution_state(
        self,
        signal: TradeSignal,
        proposal: OrderRequest,
        price: float,
        asset_class: AssetClass,
    ) -> None:
        if signal.signal == Signal.BUY:
            tranche_meta = ((proposal.metadata or {}).get("tranche") or {})
            is_valid_next_tranche = bool(tranche_meta.get("is_valid_next_tranche"))
            if is_valid_next_tranche and proposal.quantity and proposal.price:
                submitted_notional = self._round_money(
                    self._decimal(proposal.quantity) * self._decimal(proposal.price)
                )
                self.tranche_state.record_fill(
                    symbol=signal.symbol,
                    filled_notional=float(submitted_notional),
                    fill_price=float(proposal.price),
                    bar_index=self.tranche_state.get_scan_bar_index(),
                    reason=tranche_meta.get("decision_reason") or "Tranche submitted.",
                )
        elif signal.signal == Signal.SELL:
            if not self.portfolio.is_sellable_long_position(signal.symbol):
                self.tranche_state.mark_position_closed(
                    signal.symbol,
                    reason="Position closed by sell execution.",
                )
            else:
                self.tranche_state.mark_decision(
                    signal.symbol,
                    reason="Position reduced by sell execution.",
                    blocked_reason=None,
                )

    def _latest_price(self, signal: TradeSignal) -> float:
        if self.market_data_service is None:
            raise RuntimeError("Market data service is not configured for execution.")
        return self.market_data_service.get_latest_price(signal.symbol, signal.asset_class)

    def _calculate_position_size(
        self,
        signal: TradeSignal,
        asset_class: AssetClass,
        price: float,
    ) -> OrderSizing:
        raw_price = self._decimal(price)
        rounded_price = self._round_price(raw_price, asset_class)
        hard_max_notional = self._decimal(self.settings.max_position_notional)
        effective_max_notional = self._decimal(self.settings.effective_max_position_notional)
        asset = self.broker.get_asset(signal.symbol, asset_class)
        fractionable = asset.fractionable if asset else asset_class == AssetClass.CRYPTO
        scale_in_meta = (signal.metrics or {}).get("scale_in", {})

        sizing_details: dict[str, Any] = {
            "raw_price": float(raw_price),
            "rounded_price": float(rounded_price),
            "hard_max_position_notional": float(self._round_money(hard_max_notional)),
            "effective_max_order_notional": float(self._round_money(effective_max_notional)),
            "max_position_notional": float(self._round_money(hard_max_notional)),
            "comparison_operator": ">",
            "buffer_pct": float(self.settings.position_notional_buffer_pct),
            "fractionable": fractionable,
            "is_valid_next_tranche": bool(scale_in_meta.get("is_valid_next_tranche")),
            "tranche_number": scale_in_meta.get("tranche_number"),
            "tranche_count_total": scale_in_meta.get("tranche_count_total"),
            "tranche_notional": scale_in_meta.get("next_tranche_notional"),
            "scale_in_mode": scale_in_meta.get("scale_in_mode", self.settings.scale_in_mode),
            "allow_average_down": scale_in_meta.get("allow_average_down", self.settings.allow_average_down),
            "tranche_consumes_new_slot": bool(scale_in_meta.get("tranche_consumes_new_slot", True)),
            "allow_duplicate_buy_for_scale_in": bool(scale_in_meta.get("is_valid_next_tranche", False)),
        }
        existing_position = self.portfolio.get_position(signal.symbol)
        if signal.signal == Signal.SELL and self.portfolio.is_sellable_long_position(signal.symbol):
            requested_quantity = (
                signal.position_size
                if signal.position_size is not None and signal.position_size > 0
                else existing_position.quantity
            )
            sell_quantity = min(requested_quantity, existing_position.quantity)
            raw_quantity = self._decimal(sell_quantity)
            rounded_quantity = self._round_quantity(raw_quantity, fractionable=fractionable)
            final_notional = self._round_money(rounded_quantity * rounded_price)
            sizing_details.update(
                {
                    "raw_calculated_qty": float(raw_quantity),
                    "raw_notional_before_rounding": float(raw_quantity * raw_price),
                    "rounded_qty": float(rounded_quantity),
                    "rounded_quantity": float(rounded_quantity),
                    "final_submitted_notional": float(final_notional),
                    "rounded_notional": float(final_notional),
                    "max_allowed_notional": float(self._round_money(hard_max_notional)),
                    "quantity_reduced_to_fit_cap": False,
                }
            )
            return OrderSizing(
                quantity=float(rounded_quantity),
                notional=None,
                price=float(rounded_price),
                metadata={"sizing": sizing_details},
            )

        if signal.signal == Signal.SELL:
            sizing_details.update(
                {
                    "raw_calculated_qty": 0.0,
                    "raw_notional_before_rounding": 0.0,
                    "rounded_qty": 0.0,
                    "rounded_quantity": 0.0,
                    "final_submitted_notional": 0.0,
                    "rounded_notional": 0.0,
                    "max_allowed_notional": float(self._round_money(hard_max_notional)),
                    "quantity_reduced_to_fit_cap": False,
                }
            )
            return OrderSizing(
                quantity=0.0,
                notional=None,
                price=float(rounded_price),
                metadata={"sizing": sizing_details},
            )

        if signal.position_size is not None and signal.position_size > 0:
            raw_quantity = self._decimal(signal.position_size)
            requested_notional_cap = hard_max_notional
        else:
            planned_tranche_notional = self._decimal(
                scale_in_meta.get("next_tranche_notional", float(self._round_money(effective_max_notional)))
            )
            requested_notional_cap = max(Decimal("0"), min(planned_tranche_notional, effective_max_notional, hard_max_notional))
            raw_quantity = requested_notional_cap / max(rounded_price, Decimal("0.000001"))

        clamp_result = self.clamp_order_to_notional_cap(
            raw_quantity=raw_quantity,
            rounded_price=rounded_price,
            max_notional=requested_notional_cap,
            fractionable=fractionable,
        )
        sizing_details.update(
            {
                "raw_calculated_qty": float(raw_quantity),
                "raw_notional_before_rounding": float(raw_quantity * raw_price),
                "rounded_qty": float(clamp_result["rounded_quantity"]),
                "rounded_quantity": float(clamp_result["rounded_quantity"]),
                "final_submitted_notional": float(clamp_result["final_notional"]),
                "rounded_notional": float(clamp_result["final_notional"]),
                "max_allowed_notional": float(self._round_money(requested_notional_cap)),
                "quantity_reduced_to_fit_cap": bool(clamp_result["quantity_reduced_to_fit_cap"]),
                "quantity_reduction_steps": int(clamp_result["reduction_steps"]),
            }
        )
        remaining_allocation_before = self._decimal(scale_in_meta.get("remaining_allocation", 0.0))
        submitted_notional = self._decimal(clamp_result["final_notional"])
        remaining_allocation_after = max(Decimal("0"), remaining_allocation_before - submitted_notional)
        target_position_notional = self._decimal(
            scale_in_meta.get("target_position_notional", float(self._round_money(effective_max_notional)))
        )
        projected_position_notional_after_fill = max(
            Decimal("0"),
            target_position_notional - remaining_allocation_after,
        )
        return OrderSizing(
            quantity=float(clamp_result["rounded_quantity"]),
            notional=None,
            price=float(rounded_price),
            metadata={
                "sizing": sizing_details,
                "tranche": {
                    "is_valid_next_tranche": bool(scale_in_meta.get("is_valid_next_tranche")),
                    "tranche_number": scale_in_meta.get("tranche_number"),
                    "tranche_count_total": scale_in_meta.get("tranche_count_total", self.settings.entry_tranches),
                    "next_tranche_notional": float(
                        self._decimal(scale_in_meta.get("next_tranche_notional", float(clamp_result["final_notional"])))
                    ),
                    "remaining_allocation": scale_in_meta.get("remaining_allocation"),
                    "remaining_planned_allocation": float(self._round_money(remaining_allocation_after)),
                    "target_position_notional": scale_in_meta.get("target_position_notional"),
                    "projected_position_notional_after_fill": float(
                        self._round_money(projected_position_notional_after_fill)
                    ),
                    "scale_in_mode": scale_in_meta.get("scale_in_mode", self.settings.scale_in_mode),
                    "allow_average_down": scale_in_meta.get("allow_average_down", self.settings.allow_average_down),
                    "tranche_consumes_new_slot": bool(scale_in_meta.get("tranche_consumes_new_slot", True)),
                    "decision_reason": scale_in_meta.get("decision_reason"),
                },
            },
        )

    def build_initial_entry_plan(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        price: float,
    ) -> dict[str, Any]:
        total_target_notional = float(
            self._round_money(
                min(
                    self._decimal(self.settings.max_position_notional),
                    self._decimal(self.settings.effective_max_position_notional),
                )
            )
        )
        self.tranche_state.upsert_plan(
            symbol=symbol,
            asset_class=asset_class,
            target_position_notional=total_target_notional,
            tranche_weights=self.settings.entry_tranche_weights,
            scale_in_mode=self.settings.scale_in_mode,
            allow_average_down=self.settings.allow_average_down,
            decision_reason="Initial tranche plan created.",
        )
        next_tranche = self.tranche_state.compute_next_tranche(symbol) or {
            "next_tranche_number": 1,
            "next_tranche_notional": 0.0,
            "remaining_allocation": total_target_notional,
        }
        return {
            "symbol": symbol.strip().upper(),
            "asset_class": asset_class.value,
            "is_valid_next_tranche": True,
            "tranche_number": next_tranche["next_tranche_number"] or 1,
            "tranche_count_total": self.settings.entry_tranches,
            "next_tranche_notional": float(next_tranche["next_tranche_notional"]),
            "remaining_allocation": float(next_tranche["remaining_allocation"]),
            "target_position_notional": total_target_notional,
            "scale_in_mode": self.settings.scale_in_mode,
            "allow_average_down": self.settings.allow_average_down,
            "tranche_consumes_new_slot": True,
            "decision_reason": "Initial tranche approved.",
        }

    def get_next_tranche_plan(
        self,
        *,
        symbol: str,
    ) -> dict[str, Any] | None:
        plan = self.tranche_state.get_plan(symbol)
        if plan is None:
            return None
        next_tranche = self.tranche_state.compute_next_tranche(symbol)
        if next_tranche is None:
            return None
        return {
            "symbol": plan.symbol,
            "asset_class": plan.asset_class.value,
            "is_valid_next_tranche": bool(next_tranche["next_tranche_number"]),
            "tranche_number": next_tranche["next_tranche_number"],
            "tranche_count_total": plan.tranche_count_total,
            "next_tranche_notional": float(next_tranche["next_tranche_notional"]),
            "remaining_allocation": float(next_tranche["remaining_allocation"]),
            "target_position_notional": float(plan.target_position_notional),
            "scale_in_mode": plan.scale_in_mode,
            "allow_average_down": plan.allow_average_down,
            "tranche_consumes_new_slot": False,
            "decision_reason": "Next tranche candidate evaluated.",
            "last_tranche_fill_time": (
                plan.last_tranche_fill_time.isoformat() if plan.last_tranche_fill_time is not None else None
            ),
            "last_tranche_fill_bar_index": plan.last_tranche_fill_bar_index,
            "average_entry_price": plan.average_entry_price,
            "last_fill_price": plan.last_fill_price,
        }

    def can_open_initial_tranche(self, *, symbol: str) -> ScaleInDecision:
        if self.portfolio.get_position(symbol) is not None:
            return ScaleInDecision(
                approved=False,
                reason="Initial tranche blocked because a tracked position already exists.",
                rule="duplicate_position",
                details={"symbol": symbol, "is_valid_next_tranche": False},
            )
        return ScaleInDecision(
            approved=True,
            reason="Initial tranche allowed.",
            rule="initial_tranche_allowed",
        )

    def can_add_next_tranche(
        self,
        *,
        signal: TradeSignal,
        price: float,
        plan: TranchePlanState,
        next_plan: dict[str, Any],
    ) -> ScaleInDecision:
        symbol = signal.symbol.strip().upper()
        if next_plan.get("tranche_number") is None:
            return ScaleInDecision(
                approved=False,
                reason="All configured tranches are already filled for this symbol.",
                rule="tranche_plan_completed",
                details={**next_plan, "blocked_reason": "All tranches complete.", "symbol": symbol},
            )

        if signal.signal != Signal.BUY:
            return ScaleInDecision(
                approved=False,
                reason="Next tranche requires a bullish BUY confirmation.",
                rule="signal_confirmation_failed",
                details={**next_plan, "blocked_reason": "Signal is not BUY.", "symbol": symbol},
            )

        if not plan.allow_average_down and plan.average_entry_price is not None and price < plan.average_entry_price:
            return ScaleInDecision(
                approved=False,
                reason="Average-down add-on blocked by ALLOW_AVERAGE_DOWN=false.",
                rule="average_down_blocked",
                details={
                    **next_plan,
                    "blocked_reason": "Price is below average entry and averaging down is disabled.",
                    "symbol": symbol,
                    "reference_price": plan.average_entry_price,
                    "current_price": price,
                },
            )

        if plan.scale_in_mode == "time":
            now = datetime.now(timezone.utc)
            if plan.last_tranche_fill_time is not None and self.settings.minutes_between_tranches > 0:
                elapsed_minutes = (now - plan.last_tranche_fill_time).total_seconds() / 60.0
                if elapsed_minutes < self.settings.minutes_between_tranches:
                    return ScaleInDecision(
                        approved=False,
                        reason=(
                            f"Add-on blocked: wait at least {self.settings.minutes_between_tranches} minutes "
                            "between tranches."
                        ),
                        rule="tranche_time_wait",
                        details={
                            **next_plan,
                            "blocked_reason": "Time wait rule not satisfied.",
                            "symbol": symbol,
                            "elapsed_minutes": elapsed_minutes,
                            "required_minutes": self.settings.minutes_between_tranches,
                        },
                    )

            if plan.last_tranche_fill_bar_index is not None and self.settings.min_bars_between_tranches > 0:
                elapsed_bars = self.tranche_state.get_scan_bar_index() - plan.last_tranche_fill_bar_index
                if elapsed_bars < self.settings.min_bars_between_tranches:
                    return ScaleInDecision(
                        approved=False,
                        reason=(
                            f"Add-on blocked: wait at least {self.settings.min_bars_between_tranches} scan bars "
                            "between tranches."
                        ),
                        rule="tranche_bar_wait",
                        details={
                            **next_plan,
                            "blocked_reason": "Bar wait rule not satisfied.",
                            "symbol": symbol,
                            "elapsed_bars": elapsed_bars,
                            "required_bars": self.settings.min_bars_between_tranches,
                        },
                    )

        if plan.scale_in_mode == "momentum":
            reference_price = plan.last_fill_price or plan.average_entry_price
            if reference_price is None:
                reference_price = price
            favorable_multiplier = Decimal("1") + (
                self._decimal(self.settings.add_on_favorable_move_pct) / Decimal("100")
            )
            required_price = self._decimal(reference_price) * favorable_multiplier
            if self._decimal(price) < required_price:
                return ScaleInDecision(
                    approved=False,
                    reason=(
                        "Add-on blocked: favorable move threshold not met for momentum scale-in."
                    ),
                    rule="favorable_move_required",
                    details={
                        **next_plan,
                        "blocked_reason": "Favorable move rule not satisfied.",
                        "symbol": symbol,
                        "reference_price": reference_price,
                        "required_price": float(self._round_price(required_price, plan.asset_class)),
                        "current_price": price,
                        "add_on_favorable_move_pct": self.settings.add_on_favorable_move_pct,
                    },
                )

        return ScaleInDecision(
            approved=True,
            reason="Next tranche allowed.",
            rule="next_tranche_allowed",
            details={**next_plan, "blocked_reason": None, "symbol": symbol},
        )

    def clamp_order_to_notional_cap(
        self,
        *,
        raw_quantity: Decimal,
        rounded_price: Decimal,
        max_notional: Decimal,
        fractionable: bool,
    ) -> dict[str, Any]:
        quantity_quantum = FRACTIONAL_QTY_QUANTUM if fractionable else WHOLE_QTY_QUANTUM
        rounded_quantity = self._round_quantity(raw_quantity, fractionable=fractionable)
        final_notional = self._round_money(rounded_quantity * rounded_price)
        quantity_reduced = False
        reduction_steps = 0
        max_notional = self._round_money(max_notional)
        while rounded_quantity > 0 and final_notional > max_notional:
            quantity_reduced = True
            rounded_quantity = self._round_quantity(
                max(Decimal("0"), rounded_quantity - quantity_quantum),
                fractionable=fractionable,
            )
            final_notional = self._round_money(rounded_quantity * rounded_price)
            reduction_steps += 1
            if reduction_steps > 1_000_000:
                break
        return {
            "rounded_quantity": rounded_quantity,
            "final_notional": final_notional,
            "quantity_reduced_to_fit_cap": quantity_reduced,
            "reduction_steps": reduction_steps,
        }

    def build_scale_in_decision(
        self,
        *,
        signal: TradeSignal,
        asset_class: AssetClass,
        price: float,
    ) -> ScaleInDecision:
        if signal.signal != Signal.BUY:
            return ScaleInDecision(approved=True, reason="Not a BUY signal.", rule="not_buy")

        symbol = signal.symbol.strip().upper()
        can_open_initial = self.can_open_initial_tranche(symbol=symbol)
        existing_position = self.portfolio.get_position(symbol)

        if existing_position is None:
            if not can_open_initial.approved:
                return can_open_initial
            initial_plan = self.build_initial_entry_plan(symbol=symbol, asset_class=asset_class, price=price)
            return ScaleInDecision(
                approved=True,
                reason="Initial tranche approved.",
                rule="initial_tranche_allowed",
                details=initial_plan,
            )

        next_plan = self.get_next_tranche_plan(symbol=symbol)
        if next_plan is None:
            return ScaleInDecision(
                approved=True,
                reason="No active tranche plan for this existing position; duplicate BUY remains blocked.",
                rule="no_active_tranche_plan",
                details={
                    "symbol": symbol,
                    "is_valid_next_tranche": False,
                    "tranche_consumes_new_slot": False,
                    "decision_reason": "No active plan for existing position.",
                },
            )

        plan = self.tranche_state.get_plan(symbol)
        if plan is None:
            return ScaleInDecision(
                approved=False,
                reason="Tranche state missing for existing position.",
                rule="tranche_state_missing",
                details={
                    "symbol": symbol,
                    "is_valid_next_tranche": False,
                    "tranche_consumes_new_slot": False,
                },
            )

        decision = self.can_add_next_tranche(signal=signal, price=price, plan=plan, next_plan=next_plan)
        if decision.approved:
            merged_details = {
                **next_plan,
                **decision.details,
                "is_valid_next_tranche": True,
                "tranche_consumes_new_slot": False,
                "decision_reason": "Next tranche approved.",
            }
            self.tranche_state.mark_decision(
                symbol,
                reason="Next tranche approved.",
                blocked_reason=None,
                plan_status="active",
            )
            return ScaleInDecision(
                approved=True,
                reason=decision.reason,
                rule=decision.rule,
                details=merged_details,
            )

        self.tranche_state.mark_decision(
            symbol,
            reason="Next tranche blocked.",
            blocked_reason=f"{decision.rule}: {decision.reason}",
            plan_status="active",
        )
        return decision

    def has_pending_tranche(self, symbol: str) -> bool:
        plan = self.tranche_state.get_plan(symbol)
        if plan is None:
            return False
        next_plan = self.get_next_tranche_plan(symbol=symbol)
        if next_plan is None:
            return False
        return bool(next_plan.get("tranche_number")) and float(next_plan.get("next_tranche_notional", 0.0)) > 0.0

    def _evaluate_signal_risk(
        self,
        signal: TradeSignal,
        proposal: OrderRequest,
        price: float,
    ) -> RiskDecision:
        spread_pct = signal.metrics.get("spread_pct") if signal.metrics else None
        data_age_seconds = None
        if signal.generated_at:
            generated_at = signal.generated_at
            if generated_at.tzinfo is not None:
                generated_at = generated_at.astimezone(timezone.utc).replace(tzinfo=None)
            data_age_seconds = max(0.0, (datetime.utcnow() - generated_at).total_seconds())
        avg_volume = signal.metrics.get("avg_volume") if signal.metrics else None
        dollar_volume = signal.metrics.get("dollar_volume") if signal.metrics else None
        exchange = signal.metrics.get("exchange") if signal.metrics else None
        normalized_snapshot = signal.metrics.get("normalized_snapshot", {}) if signal.metrics else {}
        quantity = proposal.quantity or ((proposal.notional or 0.0) / max(price, 1e-9))
        return self.risk_manager.guard_against(
            proposal.symbol,
            proposal.side,
            quantity,
            price,
            stop_price=signal.stop_price,
            asset_class=proposal.asset_class,
            strategy_name=signal.strategy_name,
            spread_pct=spread_pct,
            avg_volume=avg_volume,
            dollar_volume=dollar_volume,
            data_age_seconds=data_age_seconds,
            exchange=exchange,
            quote_bid=normalized_snapshot.get("bid_price"),
            quote_ask=normalized_snapshot.get("ask_price"),
            quote_mid=normalized_snapshot.get("mid_price"),
            quote_timestamp=normalized_snapshot.get("quote_timestamp"),
            quote_age_seconds=normalized_snapshot.get("quote_age_seconds"),
            quote_available=normalized_snapshot.get("quote_available"),
            quote_stale=normalized_snapshot.get("quote_stale"),
            spread_abs=normalized_snapshot.get("spread_abs"),
            price_source_used=normalized_snapshot.get("price_source_used"),
            fallback_pricing_used=normalized_snapshot.get("fallback_pricing_used"),
            sizing=proposal.metadata.get("sizing") if proposal.metadata else None,
        )

    def _decimal(self, value: Any) -> Decimal:
        return Decimal(str(value))

    def _round_money(self, value: Decimal) -> Decimal:
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    def _round_price(self, value: Decimal, asset_class: AssetClass) -> Decimal:
        quantum = Decimal("0.000001") if asset_class == AssetClass.CRYPTO else PRICE_QUANTUM
        return value.quantize(quantum, rounding=ROUND_HALF_UP)

    def _round_quantity(self, value: Decimal, *, fractionable: bool) -> Decimal:
        quantum = FRACTIONAL_QTY_QUANTUM if fractionable else WHOLE_QTY_QUANTUM
        rounded = value.quantize(quantum, rounding=ROUND_DOWN)
        if not fractionable:
            rounded = Decimal(int(rounded))
        return max(Decimal("0"), rounded)

    def _annotate_signal_context(self, signal: TradeSignal) -> None:
        if signal.metrics is None:
            signal.metrics = {}

        position = self.portfolio.get_position(signal.symbol)
        has_tracked_position = position is not None
        has_tracked_long_position = self.portfolio.is_sellable_long_position(signal.symbol)
        signal.metrics.setdefault("has_tracked_position", has_tracked_position)
        signal.metrics.setdefault("has_tracked_long_position", has_tracked_long_position)
        signal.metrics.setdefault("short_selling_enabled", self.settings.short_selling_enabled)

        if signal.signal != Signal.SELL:
            return

        signal.metrics["is_risk_reducing_sell"] = has_tracked_long_position
        signal.metrics["tracked_position_quantity"] = position.quantity if position is not None else 0.0
        signal.metrics["tracked_position_side"] = str(position.side) if position is not None else None
        if has_tracked_long_position and signal.signal_type == "entry":
            signal.signal_type = "exit"

    def _persist_signal_event(self, signal: TradeSignal, price: float, quantity: float, decision: RiskDecision) -> None:
        try:
            with SessionLocal() as session:
                session.add(
                    SignalEvent(
                        symbol=signal.symbol,
                        signal=signal.signal.value,
                        strength=signal.strength,
                        price=price,
                        reason=signal.reason,
                        atr=signal.atr,
                        stop_price=signal.stop_price,
                        trailing_stop=signal.trailing_stop,
                        momentum_score=signal.momentum_score,
                        regime_state=signal.regime_state,
                    )
                )
                session.add(
                    NormalizedSignalRecord(
                        symbol=signal.symbol,
                        asset_class=signal.asset_class.value,
                        strategy_name=signal.strategy_name,
                        signal_type=signal.signal_type,
                        direction=signal.direction.value,
                        signal=signal.signal.value,
                        confidence_score=signal.confidence_score,
                        entry_price=signal.entry_price,
                        stop_price=signal.stop_price,
                        target_price=signal.target_price,
                        position_size=signal.position_size,
                        atr=signal.atr,
                        momentum_score=signal.momentum_score,
                        liquidity_score=signal.liquidity_score,
                        spread_score=signal.spread_score,
                        regime_state=signal.regime_state,
                        reason=signal.reason,
                        generated_at=signal.generated_at,
                        metrics_json=json.dumps(
                            {
                                **signal.metrics,
                                "risk_rule": decision.rule,
                                "risk_reason": decision.reason,
                                "price": price,
                                "quantity": quantity,
                            },
                            default=str,
                        ),
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist signal event: %s", exc)

    def _persist_order(self, signal: TradeSignal, order: OrderRequest, executed_order: dict[str, Any]) -> None:
        try:
            status = executed_order.get("status", "UNKNOWN")
            with SessionLocal() as session:
                session.add(
                    LegacyOrderRecord(
                        symbol=order.symbol,
                        side=order.side if isinstance(order.side, str) else str(order.side),
                        quantity=order.quantity or 0.0,
                        price=order.price,
                        status=status,
                        is_dry_run=order.is_dry_run,
                    )
                )
                session.add(
                    FillRecord(
                        order_id=str(executed_order.get("id") or executed_order.get("client_order_id") or ""),
                        symbol=order.symbol,
                        asset_class=order.asset_class.value,
                        side=order.side if isinstance(order.side, str) else str(order.side),
                        quantity=order.quantity or 0.0,
                        price=float(order.price or 0.0),
                        status=status,
                        raw_payload=json.dumps(executed_order, default=str),
                    )
                )
                if order.symbol in self.portfolio.positions:
                    position = self.portfolio.positions[order.symbol]
                    session.add(
                        PositionSnapshotRecord(
                            symbol=position.symbol,
                            asset_class=position.asset_class.value,
                            quantity=position.quantity,
                            entry_price=position.entry_price,
                            current_price=position.current_price,
                            market_value=position.quantity * position.current_price,
                            side=position.side,
                            exchange=position.exchange,
                        )
                    )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist order record: %s", exc)

    def _notify_trade_event(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        risk_decision: RiskDecision,
        order: dict[str, Any] | None = None,
    ) -> None:
        notifier = get_discord_notifier(self.settings)
        notifier.send_trade_notification(
            action=action,
            signal=signal,
            proposal=proposal,
            risk=risk_decision,
            order=order,
        )

    def run_once(self, symbol: str, strategy: BaseStrategy, data: Any) -> dict[str, Any]:
        signals = strategy.generate_signals(symbol, data)
        if not signals:
            return {"status": "no-signals", "reason": "Strategy returned no signals."}
        return self.process_signal(signals[-1])
