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
class ExecutionService:
    broker: BrokerInterface
    portfolio: Portfolio
    risk_manager: RiskManager
    dry_run: bool = True
    market_data_service: MarketDataService | None = None
    settings: Settings = field(default_factory=get_settings)

    def process_signal(self, signal: TradeSignal) -> dict[str, Any]:
        self._annotate_signal_context(signal)
        if signal.signal == Signal.HOLD:
            logger.info("Signal is HOLD", extra={"symbol": signal.symbol, "strategy": signal.strategy_name})
            return {
                "symbol": signal.symbol,
                "signal": signal.signal.value,
                "latest_price": signal.price,
                "proposal": {},
                "risk": RiskDecision(False, "No trade signal", rule="hold").to_dict(),
                "action": "hold",
                "order": None,
            }

        price = signal.entry_price or signal.price or self._latest_price(signal)
        asset_class = signal.asset_class if signal.asset_class != AssetClass.UNKNOWN else infer_asset_class(signal.symbol)
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

        sizing_details: dict[str, Any] = {
            "raw_price": float(raw_price),
            "rounded_price": float(rounded_price),
            "hard_max_position_notional": float(self._round_money(hard_max_notional)),
            "effective_max_order_notional": float(self._round_money(effective_max_notional)),
            "comparison_operator": ">",
            "buffer_pct": float(self.settings.position_notional_buffer_pct),
            "fractionable": fractionable,
        }
        existing_position = self.portfolio.get_position(signal.symbol)
        if signal.signal == Signal.SELL and self.portfolio.is_sellable_long_position(signal.symbol):
            requested_quantity = signal.position_size if signal.position_size is not None and signal.position_size > 0 else existing_position.quantity
            sell_quantity = min(requested_quantity, existing_position.quantity)
            raw_quantity = self._decimal(sell_quantity)
            rounded_quantity = self._round_quantity(raw_quantity, fractionable=fractionable)
            rounded_notional = self._round_money(rounded_quantity * rounded_price)
            sizing_details.update(
                {
                    "raw_calculated_qty": float(raw_quantity),
                    "raw_notional_before_rounding": float(raw_quantity * raw_price),
                    "rounded_quantity": float(rounded_quantity),
                    "rounded_notional": float(rounded_notional),
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
                    "rounded_quantity": 0.0,
                    "rounded_notional": 0.0,
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
        else:
            try:
                account = self.broker.get_account()
                equity = self._decimal(account.equity)
                risk_budget = equity * self._decimal(self.settings.max_risk_per_trade)
                if signal.stop_price:
                    stop_distance = rounded_price - self._decimal(signal.stop_price)
                    if stop_distance > 0:
                        raw_quantity = risk_budget / stop_distance
                    else:
                        raw_quantity = effective_max_notional / max(rounded_price, Decimal("0.000001"))
                else:
                    raw_quantity = effective_max_notional / max(rounded_price, Decimal("0.000001"))
            except Exception:
                raw_quantity = effective_max_notional / max(rounded_price, Decimal("0.000001"))

        max_quantity = effective_max_notional / max(rounded_price, Decimal("0.000001"))
        capped_quantity = min(raw_quantity, max_quantity)
        rounded_quantity = self._round_quantity(capped_quantity, fractionable=fractionable)
        rounded_notional = self._round_money(rounded_quantity * rounded_price)
        sizing_details.update(
            {
                "raw_calculated_qty": float(raw_quantity),
                "raw_notional_before_rounding": float(raw_quantity * raw_price),
                "rounded_quantity": float(rounded_quantity),
                "rounded_notional": float(rounded_notional),
            }
        )
        return OrderSizing(
            quantity=float(rounded_quantity),
            notional=None,
            price=float(rounded_price),
            metadata={"sizing": sizing_details},
        )

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
