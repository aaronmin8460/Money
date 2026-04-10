from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from app.services.market_data import MarketDataService
from app.strategies.base import BaseStrategy, Signal, TradeSignal


logger = get_logger("execution")


@dataclass
class ExecutionService:
    broker: BrokerInterface
    portfolio: Portfolio
    risk_manager: RiskManager
    dry_run: bool = True
    market_data_service: MarketDataService | None = None
    settings: Settings = field(default_factory=get_settings)

    def process_signal(self, signal: TradeSignal) -> dict[str, Any]:
        if signal.signal == Signal.HOLD:
            logger.info("Signal is HOLD", extra={"symbol": signal.symbol, "strategy": signal.strategy_name})
            return {
                "symbol": signal.symbol,
                "signal": signal.signal.value,
                "latest_price": signal.price,
                "proposal": {},
                "risk": {"approved": False, "reason": "No trade signal", "rule": "hold"},
                "action": "hold",
                "order": None,
            }

        price = signal.entry_price or signal.price or self._latest_price(signal)
        asset_class = signal.asset_class if signal.asset_class != AssetClass.UNKNOWN else AssetClass.EQUITY
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
                "risk": {
                    "approved": False,
                    "reason": risk_decision.reason,
                    "rule": risk_decision.rule,
                    "details": risk_decision.details,
                },
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
            "risk": {
                "approved": True,
                "reason": risk_decision.reason,
                "rule": risk_decision.rule,
                "details": risk_decision.details,
            },
            "action": action,
            "order": executed_order,
        }

    def _build_order_request(self, signal: TradeSignal, asset_class: AssetClass, price: float) -> OrderRequest:
        quantity, notional = self._calculate_position_size(signal, asset_class, price)
        tif = "gtc" if asset_class == AssetClass.CRYPTO else "day"
        return OrderRequest(
            symbol=signal.symbol,
            side=signal.signal.value,
            quantity=quantity,
            notional=notional,
            asset_class=asset_class,
            price=price,
            time_in_force=tif,
            is_dry_run=self.dry_run or not self.settings.trading_enabled,
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
    ) -> tuple[float | None, float | None]:
        if signal.position_size is not None and signal.position_size > 0:
            return signal.position_size, None

        try:
            account = self.broker.get_account()
            equity = float(account.equity)
            risk_budget = equity * self.settings.max_risk_per_trade
            if signal.stop_price:
                stop_distance = price - signal.stop_price
                if stop_distance > 0:
                    quantity = max(1.0, risk_budget / stop_distance)
                else:
                    quantity = max(1.0, self.settings.max_notional_per_position / max(price, 1.0))
            else:
                quantity = max(1.0, self.settings.max_notional_per_position / max(price, 1.0))
        except Exception:
            quantity = max(1.0, self.settings.max_notional_per_position / max(price, 1.0))

        asset = self.broker.get_asset(signal.symbol, asset_class)
        fractionable = asset.fractionable if asset else asset_class == AssetClass.CRYPTO
        capped_quantity = min(quantity, self.settings.max_notional_per_position / max(price, 1.0))
        if fractionable:
            rounded = round(capped_quantity, 6)
            if asset_class == AssetClass.CRYPTO:
                return None, round(rounded * price, 2)
            return rounded, None
        return max(1, int(capped_quantity)), None

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
        )

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
