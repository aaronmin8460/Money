from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

from app.config.settings import get_settings, Settings
from app.portfolio.portfolio import Portfolio


@dataclass
class RiskDecision:
    approved: bool
    reason: str


class RiskManager:
    def __init__(self, portfolio: "Portfolio", settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.portfolio = portfolio

    def evaluate_order(self, symbol: str, side: str, quantity: float, price: float) -> RiskDecision:
        if not self.settings.trading_enabled:
            return RiskDecision(True, "Trading is disabled. The order will be evaluated as a dry-run.")

        if quantity <= 0 or price <= 0:
            return RiskDecision(False, "Invalid order quantity or price.")

        if len(self.portfolio.positions) >= self.settings.max_positions and side.upper() == "BUY":
            return RiskDecision(False, f"Maximum simultaneous positions ({self.settings.max_positions}) reached.")

        risk_amount = quantity * price * self.settings.max_risk_per_trade
        if risk_amount > self.portfolio.cash * self.settings.max_risk_per_trade:
            return RiskDecision(False, f"Order risk ({risk_amount:.2f}) exceeds max risk per trade ({self.portfolio.cash * self.settings.max_risk_per_trade:.2f}).")

        if self.portfolio.drawdown_pct() >= self.settings.max_drawdown_pct:
            return RiskDecision(False, f"Max drawdown ({self.portfolio.drawdown_pct():.2%}) exceeded ({self.settings.max_drawdown_pct:.2%}).")

        if self.portfolio.daily_loss_pct() >= self.settings.max_daily_loss_pct:
            return RiskDecision(False, f"Max daily loss ({self.portfolio.daily_loss_pct():.2%}) reached ({self.settings.max_daily_loss_pct:.2%}).")

        # Prevent duplicate buy orders for the same symbol.
        if side.upper() == "BUY" and symbol in self.portfolio.positions:
            return RiskDecision(False, "Duplicate buy order blocked for existing position.")

        # Check market open for Alpaca
        if self.settings.is_alpaca_mode and not self.settings.allow_extended_hours:
            from app.services.broker import create_broker
            broker = create_broker(self.settings)
            if hasattr(broker, 'is_market_open') and not broker.is_market_open():
                return RiskDecision(False, "Market is closed and extended hours not allowed.")

        return RiskDecision(True, "Order approved by risk manager.")

    def record_event(self, symbol: Optional[str], reason: str, details: Optional[str] = None) -> None:
        self.portfolio.risk_events.append({
            "symbol": symbol,
            "reason": reason,
            "details": details,
        })

    def guard_against(self, symbol: str, side: str, quantity: float, price: float) -> RiskDecision:
        decision = self.evaluate_order(symbol, side, quantity, price)
        if not decision.approved:
            self.record_event(symbol, decision.reason, f"side={side}, qty={quantity}, price={price}")
        return decision
