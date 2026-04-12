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
from app.domain.models import AssetClass, SessionState, SignalDirection
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.events import build_signal_id
from app.monitoring.logger import get_logger
from app.monitoring.outcome_logger import get_outcome_logger
from app.monitoring.trade_logger import get_trade_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskDecision, RiskManager
from app.services.broker import BrokerInterface, OrderRequest
from app.services.market_data import MarketDataService, infer_asset_class
from app.services.tranche_state import TranchePlanState, TrancheStateStore
from app.strategies.base import (
    ENTRY_ORDER_INTENTS,
    EXIT_ORDER_INTENTS,
    BaseStrategy,
    Signal,
    TradeSignal,
    resolve_signal_direction,
)


logger = get_logger("execution")

MONEY_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.0001")
FRACTIONAL_QTY_QUANTUM = Decimal("0.000001")
WHOLE_QTY_QUANTUM = Decimal("1")
ORDER_INTENT_TO_BROKER_SIDE = {
    "long_entry": Signal.BUY.value,
    "long_exit": Signal.SELL.value,
    "short_entry": Signal.SELL.value,
    "short_exit": Signal.BUY.value,
}


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
            decision_code = str((signal.metrics or {}).get("decision_code") or "no_signal")
            hold_action = (
                "skipped"
                if decision_code in {
                    "market_closed",
                    "market_closed",
                    "market_closed_extended_hours_disabled",
                    "extended_hours_not_supported_for_asset",
                    "no_position_to_sell",
                    "no_position_to_cover",
                    "short_selling_disabled",
                    "skipped_low_ml_score",
                    "ml_inference_error",
                }
                else "hold"
            )
            hold_risk = RiskDecision(
                approved=False,
                reason=signal.reason or "No trade signal.",
                rule=decision_code,
                details={
                    **(signal.metrics or {}),
                    "latest_price": latest_price,
                    "action": hold_action,
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
            self._log_execution_artifacts(
                action=hold_action,
                signal=signal,
                proposal=hold_proposal,
                risk_decision=hold_risk,
                order=None,
            )
            logger.info(
                "Signal not traded",
                extra={
                    "symbol": signal.symbol,
                    "strategy": signal.strategy_name,
                    "action": hold_action,
                    "rule": decision_code,
                },
            )
            return {
                "symbol": signal.symbol,
                "signal": signal.signal.value,
                "latest_price": latest_price,
                "proposal": {},
                "risk": hold_risk.to_dict(),
                "action": hold_action,
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
            self._log_execution_artifacts(
                action="rejected",
                signal=signal,
                proposal=rejected_proposal,
                risk_decision=risk_decision,
                order=None,
            )
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
        
        # Attempt quantity reduction if stop-based risk exceeds max_risk_per_trade.
        if self._is_exposure_increasing_signal(signal) and signal.stop_price is not None:
            proposal = self._attempt_risk_compliant_sizing(signal, proposal, price, asset_class)
        
        risk_decision = self._evaluate_signal_risk(signal, proposal, price)
        self._persist_signal_event(signal, price, proposal.quantity or 0.0, risk_decision)

        proposal_payload = self._proposal_to_payload(proposal, asset_class)

        if not risk_decision.approved:
            logger.warning("Order blocked by risk manager", extra={"reason": risk_decision.reason, "symbol": proposal.symbol})
            if self._is_long_entry_signal(signal):
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
            self._log_execution_artifacts(
                action="rejected",
                signal=signal,
                proposal=proposal,
                risk_decision=risk_decision,
                order=None,
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

        proposal, session_skip = self._apply_session_submission_rules(
            signal=signal,
            proposal=proposal,
            asset_class=asset_class,
            price=price,
        )
        proposal_payload = self._proposal_to_payload(proposal, asset_class)
        if session_skip is not None:
            logger.info(
                "Order skipped by session rules",
                extra={
                    "symbol": proposal.symbol,
                    "rule": session_skip.rule,
                    "reason": session_skip.reason,
                },
            )
            self._log_execution_artifacts(
                action="skipped",
                signal=signal,
                proposal=proposal,
                risk_decision=session_skip,
                order=None,
            )
            return {
                "symbol": proposal.symbol,
                "signal": signal.signal.value,
                "latest_price": price,
                "proposal": proposal_payload,
                "risk": session_skip.to_dict(),
                "action": "skipped",
                "order": None,
            }

        self.portfolio.mark_to_market({proposal.symbol: price})
        executed_order = self.broker.submit_order(proposal)
        if not proposal.is_dry_run:
            self.portfolio.update_position(
                proposal.symbol,
                proposal.side if isinstance(proposal.side, str) else str(proposal.side),
                proposal.quantity or 0.0,
                price,
                asset_class=asset_class,
                exchange=signal.metrics.get("exchange") if signal.metrics else None,
                order_intent=signal.order_intent,
                reduce_only=signal.reduce_only,
                exit_stage=signal.exit_stage,
                signal_metadata=self._build_position_signal_metadata(signal),
            )
            self.risk_manager.mark_executed(proposal.symbol, signal.strategy_name)
            self._record_post_execution_state(signal, proposal, price, asset_class)
        self._persist_order(signal, proposal, executed_order)

        action = "dry_run" if proposal.is_dry_run else "submitted"
        logger.info("Order processed", extra={"action": action, "order": executed_order})
        self._log_execution_artifacts(
            action=action,
            signal=signal,
            proposal=proposal,
            risk_decision=risk_decision,
            order=executed_order,
        )
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

    def _proposal_to_payload(self, proposal: OrderRequest, asset_class: AssetClass) -> dict[str, Any]:
        return {
            "symbol": proposal.symbol,
            "asset_class": asset_class.value,
            "side": proposal.side.value if hasattr(proposal.side, "value") else str(proposal.side),
            "quantity": proposal.quantity,
            "notional": proposal.notional,
            "price": proposal.price,
            "time_in_force": proposal.time_in_force,
            "order_type": proposal.order_type,
            "is_dry_run": proposal.is_dry_run,
            "extended_hours": bool((proposal.metadata or {}).get("extended_hours")),
            "order_intent": (proposal.metadata or {}).get("order_intent"),
            "position_direction": (proposal.metadata or {}).get("position_direction"),
            "reduce_only": bool((proposal.metadata or {}).get("reduce_only")),
            "exit_stage": (proposal.metadata or {}).get("exit_stage"),
        }

    def _resolve_order_side(self, signal: TradeSignal) -> str:
        mapped_side = ORDER_INTENT_TO_BROKER_SIDE.get(signal.order_intent)
        if mapped_side is not None:
            return mapped_side
        return signal.signal.value

    def _resolve_position_direction(self, signal: TradeSignal) -> str | None:
        direction = resolve_signal_direction(signal.order_intent)
        return None if direction == SignalDirection.FLAT else direction.value

    def _is_exposure_reducing_signal(self, signal: TradeSignal) -> bool:
        return signal.order_intent in EXIT_ORDER_INTENTS or signal.reduce_only

    def _is_exposure_increasing_signal(self, signal: TradeSignal) -> bool:
        return signal.order_intent in ENTRY_ORDER_INTENTS and not signal.reduce_only

    def _is_long_entry_signal(self, signal: TradeSignal) -> bool:
        return signal.order_intent == "long_entry"

    def _build_order_metadata(
        self,
        signal: TradeSignal,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        combined_metadata = dict(metadata or {})
        combined_metadata["signal_type"] = signal.signal_type
        combined_metadata["order_intent"] = signal.order_intent
        combined_metadata["position_direction"] = self._resolve_position_direction(signal)
        combined_metadata["reduce_only"] = signal.reduce_only
        combined_metadata["exit_stage"] = signal.exit_stage
        combined_metadata["exit_fraction"] = signal.exit_fraction
        return combined_metadata

    def _build_position_signal_metadata(self, signal: TradeSignal) -> dict[str, Any]:
        metrics = signal.metrics or {}
        return {
            "strategy_name": signal.strategy_name,
            "signal_type": signal.signal_type,
            "order_intent": signal.order_intent,
            "position_direction": self._resolve_position_direction(signal),
            "reduce_only": signal.reduce_only,
            "exit_stage": signal.exit_stage,
            "exit_fraction": signal.exit_fraction,
            "stop_price": signal.stop_price,
            "target_price": signal.target_price,
            "trailing_stop": signal.trailing_stop,
            "current_stop": metrics.get("current_stop"),
            "next_stop": metrics.get("next_stop"),
            "tp1_price": metrics.get("tp1_price"),
            "tp2_price": metrics.get("tp2_price"),
        }

    def _attempt_risk_compliant_sizing(
        self,
        signal: TradeSignal,
        proposal: OrderRequest,
        price: float,
        asset_class: AssetClass,
    ) -> OrderRequest:
        """
        Attempt to reduce order quantity if it violates max_risk_per_trade.
        
        If the original quantity causes stop-based risk to exceed the limit,
        but a smaller quantity would be compliant, reduce the quantity and 
        record the reduction in metadata.
        """
        if proposal.quantity <= 0 or signal.stop_price is None:
            return proposal
        
        account = self.risk_manager.get_account_snapshot()
        max_trade_risk = self._round_money(
            self._decimal(account["equity"]) * self._decimal(self.settings.max_risk_per_trade)
        )
        risk_per_share = (
            signal.stop_price - price
            if signal.order_intent == "short_entry"
            else price - signal.stop_price
        )
        if risk_per_share <= 0:
            return proposal
        
        original_quantity = self._decimal(proposal.quantity)
        trade_risk = self._round_money(original_quantity * self._decimal(risk_per_share))
        
        if trade_risk <= max_trade_risk:
            return proposal
        
        # Calculate maximum compliant quantity
        max_compliant_quantity = self._round_money(max_trade_risk / self._decimal(risk_per_share))
        max_compliant_quantity = self._decimal(max_compliant_quantity)
        
        # Round down to whole/fractional quantity
        asset = self.broker.get_asset(signal.symbol, asset_class)
        fractionable = asset.fractionable if asset else asset_class == AssetClass.CRYPTO
        reduced_quantity = self._round_quantity(max_compliant_quantity, fractionable=fractionable)
        
        if reduced_quantity <= 0:
            return proposal
        
        # Create new rounded proposal with reduced quantity
        reduced_price = self._round_price(
            self._decimal(proposal.price or price),
            asset_class,
        )
        reduced_notional = self._round_money(reduced_quantity * reduced_price)
        
        # Update metadata to record the reduction
        updated_metadata = dict(proposal.metadata or {})
        updated_sizing = dict(updated_metadata.get("sizing", {}))
        updated_sizing.update({
            "original_quantity": float(original_quantity),
            "original_trade_risk": float(trade_risk),
            "reduced_quantity": float(reduced_quantity),
            "quantity_reduction_reason": "max_risk_per_trade",
            "max_trade_risk": float(max_trade_risk),
            "risk_per_share": float(risk_per_share),
            "quantity_reduction_applied": True,
        })
        updated_metadata["sizing"] = updated_sizing
        
        return OrderRequest(
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=float(reduced_quantity),
            notional=None,
            asset_class=proposal.asset_class,
            price=float(reduced_price),
            time_in_force=proposal.time_in_force,
            is_dry_run=proposal.is_dry_run,
            metadata=updated_metadata,
        )

    def _build_order_request(self, signal: TradeSignal, asset_class: AssetClass, price: float) -> OrderRequest:
        sizing = self._calculate_position_size(signal, asset_class, price)
        tif = "gtc" if asset_class == AssetClass.CRYPTO else "day"
        return OrderRequest(
            symbol=signal.symbol,
            side=self._resolve_order_side(signal),
            quantity=sizing.quantity,
            notional=sizing.notional,
            asset_class=asset_class,
            price=sizing.price,
            time_in_force=tif,
            is_dry_run=self.dry_run or not self.settings.trading_enabled,
            metadata=self._build_order_metadata(signal, sizing.metadata),
        )

    def _apply_session_submission_rules(
        self,
        *,
        signal: TradeSignal,
        proposal: OrderRequest,
        asset_class: AssetClass,
        price: float,
    ) -> tuple[OrderRequest, RiskDecision | None]:
        # The mock broker replays local CSV data and is not tied to real exchange sessions.
        if self.settings.is_mock_mode:
            return proposal, None
        if asset_class == AssetClass.CRYPTO:
            return proposal, None
        if asset_class not in {AssetClass.EQUITY, AssetClass.ETF}:
            return proposal, None
        if self.market_data_service is None:
            return proposal, None

        session = self.market_data_service.get_session_status(asset_class)
        session_state = getattr(session.session_state, "value", str(session.session_state))
        is_regular_session = session_state == SessionState.REGULAR.value
        if is_regular_session:
            return proposal, None

        if not self.settings.allow_extended_hours:
            return proposal, RiskDecision(
                approved=False,
                reason="Market is closed and extended-hours trading is disabled.",
                rule="market_closed_extended_hours_disabled",
                details={
                    "symbol": signal.symbol,
                    "asset_class": asset_class.value,
                    "session_state": session_state,
                    "allow_extended_hours": self.settings.allow_extended_hours,
                },
            )

        if not bool(session.extended_hours):
            return proposal, RiskDecision(
                approved=False,
                reason="Market session is closed and no extended-hours session is available.",
                rule="market_closed",
                details={
                    "symbol": signal.symbol,
                    "asset_class": asset_class.value,
                    "session_state": session_state,
                    "allow_extended_hours": self.settings.allow_extended_hours,
                },
            )

        asset = self.broker.get_asset(signal.symbol, asset_class)
        if asset is not None and not asset.tradable:
            return proposal, RiskDecision(
                approved=False,
                reason="Asset is not eligible for extended-hours order submission.",
                rule="extended_hours_not_supported_for_asset",
                details={
                    "symbol": signal.symbol,
                    "asset_class": asset_class.value,
                    "session_state": session_state,
                    "asset_tradable": asset.tradable,
                },
            )

        limit_price = proposal.price or signal.entry_price or signal.price or price
        if limit_price is None or limit_price <= 0:
            return proposal, RiskDecision(
                approved=False,
                reason="Extended-hours order requires a valid limit price.",
                rule="extended_hours_not_supported_for_asset",
                details={
                    "symbol": signal.symbol,
                    "asset_class": asset_class.value,
                    "session_state": session_state,
                    "limit_price": limit_price,
                },
            )

        updated_metadata = dict(proposal.metadata or {})
        updated_metadata["extended_hours"] = True
        updated_metadata["extended_hours_submission"] = {
            "session_state": session_state,
            "allow_extended_hours": self.settings.allow_extended_hours,
            "order_type": "limit",
            "time_in_force": "day",
        }
        return (
            OrderRequest(
                symbol=proposal.symbol,
                side=proposal.side,
                quantity=proposal.quantity,
                notional=proposal.notional,
                asset_class=proposal.asset_class,
                price=float(limit_price),
                time_in_force="day",
                order_type="limit",
                is_dry_run=proposal.is_dry_run,
                metadata=updated_metadata,
            ),
            None,
        )

    def _record_post_execution_state(
        self,
        signal: TradeSignal,
        proposal: OrderRequest,
        price: float,
        asset_class: AssetClass,
    ) -> None:
        if signal.order_intent == "long_entry":
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
        elif signal.order_intent == "long_exit":
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
        if signal.order_intent == "long_exit" and self.portfolio.is_sellable_long_position(signal.symbol):
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

        if signal.order_intent == "short_exit" and self.portfolio.is_coverable_short_position(signal.symbol):
            requested_quantity = (
                signal.position_size
                if signal.position_size is not None and signal.position_size > 0
                else existing_position.quantity
            )
            cover_quantity = min(requested_quantity, existing_position.quantity)
            raw_quantity = self._decimal(cover_quantity)
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

        if signal.order_intent in EXIT_ORDER_INTENTS:
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
        if signal.order_intent not in {None, "long_entry"}:
            return ScaleInDecision(approved=True, reason="Not a long-entry signal.", rule="not_long_entry")
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
        asset = None
        if hasattr(self.broker, "get_asset"):
            asset = self.broker.get_asset(proposal.symbol, proposal.asset_class)
        return self.risk_manager.guard_against(
            proposal.symbol,
            proposal.side,
            quantity,
            price,
            stop_price=signal.stop_price,
            order_intent=signal.order_intent,
            reduce_only=signal.reduce_only,
            exit_stage=signal.exit_stage,
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
            asset_metadata=asset,
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
        signal.metrics.setdefault("signal_id", build_signal_id(signal.symbol, signal.strategy_name, signal.generated_at))
        signal.apply_intent_defaults()

        position = self.portfolio.get_position(signal.symbol)
        has_tracked_position = position is not None
        has_tracked_long_position = self.portfolio.is_sellable_long_position(signal.symbol)
        has_coverable_short_position = self.portfolio.is_coverable_short_position(signal.symbol)
        signal.metrics.setdefault("has_tracked_position", has_tracked_position)
        signal.metrics.setdefault("has_tracked_long_position", has_tracked_long_position)
        signal.metrics.setdefault("has_sellable_long_position", has_tracked_long_position)
        signal.metrics.setdefault("has_coverable_short_position", has_coverable_short_position)
        signal.metrics.setdefault("short_selling_enabled", self.settings.short_selling_enabled)
        signal.metrics["tracked_position_quantity"] = position.quantity if position is not None else 0.0
        signal.metrics["tracked_position_side"] = str(position.side) if position is not None else None
        signal.metrics["tracked_position_direction"] = (
            position.direction.value if position is not None else None
        )

        if signal.signal == Signal.BUY:
            if has_coverable_short_position:
                signal.signal_type = "exit"
                signal.order_intent = "short_exit"
                signal.reduce_only = True
            elif signal.order_intent == "short_exit":
                signal.signal_type = "exit"
                signal.reduce_only = True
            else:
                signal.signal_type = "entry"
                signal.order_intent = "long_entry"
                signal.reduce_only = False
        elif signal.signal == Signal.SELL:
            if has_tracked_long_position:
                signal.signal_type = "exit"
                signal.order_intent = "long_exit"
                signal.reduce_only = True
            elif signal.order_intent == "short_entry" or self.settings.short_selling_enabled:
                signal.signal_type = "entry"
                signal.order_intent = "short_entry"
                signal.reduce_only = False
            else:
                signal.signal_type = "exit"
                signal.order_intent = "long_exit"
                signal.reduce_only = True

        signal.apply_intent_defaults()
        signal.metrics["position_direction"] = self._resolve_position_direction(signal)
        signal.metrics["is_risk_reducing_order"] = self._is_exposure_reducing_signal(signal)
        signal.metrics["is_risk_reducing_sell"] = (
            signal.signal == Signal.SELL and signal.metrics["is_risk_reducing_order"]
        )
        signal.metrics["order_intent"] = signal.order_intent
        signal.metrics["reduce_only"] = signal.reduce_only
        signal.metrics["exit_stage"] = signal.exit_stage
        signal.metrics["exit_fraction"] = signal.exit_fraction

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
                                "order_intent": signal.order_intent,
                                "position_direction": self._resolve_position_direction(signal),
                                "reduce_only": signal.reduce_only,
                                "exit_stage": signal.exit_stage,
                                "exit_fraction": signal.exit_fraction,
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
        try:
            classification = decision.rule if signal.signal == Signal.HOLD else None
            get_trade_logger(self.settings).log_signal(
                signal,
                latest_price=price,
                outcome_classification=classification,
                market_overview=dict((signal.metrics or {}).get("market_overview") or {}),
                news_features=dict((signal.metrics or {}).get("news_features") or {}),
            )
        except Exception as exc:
            logger.warning("Failed to write structured signal log: %s", exc)

    def _persist_order(self, signal: TradeSignal, order: OrderRequest, executed_order: dict[str, Any]) -> None:
        try:
            status = executed_order.get("status", "UNKNOWN")
            persisted_payload = {
                **executed_order,
                "metadata": {
                    **dict(executed_order.get("metadata") or {}),
                    **dict(order.metadata or {}),
                },
            }
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
                        raw_payload=json.dumps(persisted_payload, default=str),
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
                            market_value=self.portfolio.position_market_value(position.symbol) or 0.0,
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

    def _log_execution_artifacts(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        risk_decision: RiskDecision,
        order: dict[str, Any] | None,
    ) -> None:
        market_overview = dict((signal.metrics or {}).get("market_overview") or {})
        news_features = dict((signal.metrics or {}).get("news_features") or {})
        try:
            if action != "hold":
                get_trade_logger(self.settings).log_order(
                    action=action,
                    signal=signal,
                    proposal=proposal,
                    risk=risk_decision,
                    order=order,
                )
        except Exception as exc:
            logger.warning("Failed to write structured order log: %s", exc)
        try:
            get_outcome_logger(self.settings).log_execution_outcome(
                action=action,
                signal=signal,
                proposal=proposal,
                risk=risk_decision,
                order=order,
                market_overview=market_overview,
                news_features=news_features,
            )
        except Exception as exc:
            logger.warning("Failed to write structured outcome log: %s", exc)

    def run_once(self, symbol: str, strategy: BaseStrategy, data: Any) -> dict[str, Any]:
        signals = strategy.generate_signals(symbol, data)
        if not signals:
            return {"status": "no-signals", "reason": "Strategy returned no signals."}
        return self.process_signal(signals[-1])
