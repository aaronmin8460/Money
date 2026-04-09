from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import get_settings
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import BrokerInterface, OrderRequest
from app.services.market_data import CSVMarketDataService
from app.strategies.base import BaseStrategy, TradeSignal


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
        quantity = self._calculate_position_size(signal.symbol, price)
        order = OrderRequest(
            symbol=signal.symbol,
            side=signal.signal,
            quantity=quantity,
            price=price,
            is_dry_run=self.dry_run or not settings.trading_enabled,
        )

        decision = self.risk_manager.guard_against(order.symbol, order.side, order.quantity, order.price)
        proposal = {
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "is_dry_run": order.is_dry_run,
        }

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
            }

        self.portfolio.mark_to_market({order.symbol: price})
        executed_order = self.broker.submit_order(order)
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

    def _calculate_position_size(self, symbol: str, current_price: float) -> int:
        """Calculate position size based on risk and available cash."""
        settings = get_settings()
        try:
            max_quantity = int(settings.max_position_notional // current_price)
            # Bound by buying power
            account = self.broker.get_account()
            buying_power = account.buying_power
            max_by_bp = int(buying_power // current_price)
            quantity = min(max_quantity, max_by_bp, 1000)  # Cap at 1000 for safety
            return max(quantity, 0)
        except Exception:
            return 0

    def run_once(self, symbol: str, strategy: BaseStrategy, data: Any) -> dict[str, Any]:
        signals = strategy.generate_signals(symbol, data)
        if not signals:
            return {"status": "no-signals", "reason": "Strategy returned no signals."}
        return self.process_signal(signals[-1])
