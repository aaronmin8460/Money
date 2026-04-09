from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config.settings import get_settings
from app.db.models import Order as OrderRecord, SignalEvent
from app.db.session import SessionLocal
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import BrokerInterface, OrderRequest
from app.services.market_data import CSVMarketDataService
from app.strategies.base import BaseStrategy, Signal, TradeSignal


logger = get_logger("execution")


@dataclass
class ExecutionService:
    broker: BrokerInterface
    portfolio: Portfolio
    risk_manager: RiskManager
    dry_run: bool = True
    market_data_service: CSVMarketDataService = CSVMarketDataService()

    def process_signal(self, signal: TradeSignal) -> dict[str, Any]:
        settings = get_settings()
        if signal.signal == "HOLD":
            logger.info("Signal is HOLD", extra={"symbol": signal.symbol})
            return {
                "symbol": signal.symbol,
                "signal": signal.signal,
                "latest_price": None,
                "proposal": {},
                "risk": {"approved": False, "reason": "No trade signal"},
                "action": "hold",
                "order": None,
            }

        price = signal.price or self.market_data_service.get_latest_price(signal.symbol)
        if signal.signal == Signal.SELL:
            position = self.portfolio.positions.get(signal.symbol)
            quantity = int(position.quantity) if position and position.quantity > 0 else 0
        else:
            quantity = self._calculate_position_size(signal)

        order = OrderRequest(
            symbol=signal.symbol,
            side=signal.signal,
            quantity=quantity,
            price=price,
            is_dry_run=self.dry_run or not settings.trading_enabled,
        )

        if order.quantity <= 0:
            return {
                "symbol": order.symbol,
                "signal": signal.signal,
                "latest_price": price,
                "proposal": {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "price": order.price,
                    "is_dry_run": order.is_dry_run,
                },
                "risk": {"approved": False, "reason": "Zero quantity calculated for order."},
                "action": "rejected",
                "order": None,
            }

        decision = self.risk_manager.guard_against(order.symbol, order.side, order.quantity, order.price)
        proposal = {
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "is_dry_run": order.is_dry_run,
        }

        self._persist_signal_event(signal, price, order.quantity, decision)

        if not decision.approved:
            logger.warning(
                "Order blocked by risk manager",
                extra={"reason": decision.reason, "symbol": order.symbol},
            )
            return {
                "symbol": order.symbol,
                "signal": signal.signal,
                "latest_price": price,
                "proposal": proposal,
                "risk": {"approved": False, "reason": decision.reason},
                "action": "rejected",
                "order": None,
            }

        self.portfolio.mark_to_market({order.symbol: price})
        executed_order = self.broker.submit_order(order)
        self._persist_order(order, executed_order)
        if not order.is_dry_run:
            self.portfolio.update_position(order.symbol, order.side, order.quantity, price)

        action = "dry_run" if order.is_dry_run else "submitted"
        logger.info("Order processed", extra={"action": action, "order": executed_order})

        return {
            "symbol": order.symbol,
            "signal": signal.signal,
            "latest_price": price,
            "proposal": proposal,
            "risk": {"approved": True, "reason": decision.reason},
            "action": action,
            "order": executed_order,
        }

    def _calculate_position_size(self, signal: TradeSignal) -> int:
        """Calculate position size based on risk, stop distance, and available capital."""
        settings = get_settings()
        if not signal.price or not signal.stop_price:
            # Fallback to basic sizing
            return self._basic_position_size(signal.symbol, signal.price or 0)

        stop_distance = signal.price - signal.stop_price
        if stop_distance <= 0:
            return 0  # Invalid stop

        try:
            account = self.broker.get_account()
            equity = account.equity
            risk_budget = equity * settings.max_risk_per_trade
            shares = int(risk_budget // stop_distance)

            # Cap by max position notional
            max_by_notional = int(settings.max_position_notional // signal.price)
            shares = min(shares, max_by_notional)

            # Cap by buying power
            max_by_bp = int(account.buying_power // signal.price)
            shares = min(shares, max_by_bp)

            # Minimum 1 share
            return max(shares, 1)
        except Exception:
            return 1

    def _basic_position_size(self, symbol: str, current_price: float) -> int:
        """Fallback position sizing."""
        settings = get_settings()
        try:
            max_quantity = int(settings.max_position_notional // current_price)
            account = self.broker.get_account()
            buying_power = account.buying_power
            max_by_bp = int(buying_power // current_price)
            quantity = min(max_quantity, max_by_bp, 1000)
            return max(quantity, 1)
        except Exception:
            return 1

    def _persist_signal_event(self, signal: TradeSignal, price: float, quantity: float, decision: Any) -> None:
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
                session.commit()
        except Exception:
            logger.warning("Failed to persist signal event")

    def _persist_order(self, order: OrderRequest, executed_order: dict[str, Any]) -> None:
        try:
            with SessionLocal() as session:
                session.add(
                    OrderRecord(
                        symbol=order.symbol,
                        side=order.side.value if hasattr(order.side, "value") else str(order.side),
                        quantity=order.quantity,
                        price=order.price,
                        status=executed_order.get("status", "UNKNOWN"),
                        is_dry_run=order.is_dry_run,
                    )
                )
                session.commit()
        except Exception:
            logger.warning("Failed to persist order record")

    def run_once(self, symbol: str, strategy: BaseStrategy, data: Any) -> dict[str, Any]:
        signals = strategy.generate_signals(symbol, data)
        if not signals:
            return {"status": "no-signals", "reason": "Strategy returned no signals."}
        return self.process_signal(signals[-1])
