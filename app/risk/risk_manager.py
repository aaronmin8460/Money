from __future__ import annotations

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
            return RiskDecision(False, "Maximum simultaneous positions reached.")

        risk_amount = quantity * price * self.settings.max_risk_per_trade
        if risk_amount > self.portfolio.cash * self.settings.max_risk_per_trade:
            return RiskDecision(False, "Order exceeds max risk per trade.")

        if self.portfolio.drawdown_pct() >= self.settings.max_drawdown_pct:
            return RiskDecision(False, "Max drawdown exceeded.")

        if self.portfolio.daily_loss_pct() >= self.settings.max_daily_loss_pct:
            return RiskDecision(False, "Max daily loss limit reached.")

        # Prevent duplicate buy orders for the same symbol.
        if side.upper() == "BUY" and symbol in self.portfolio.positions:
            return RiskDecision(False, "Duplicate order blocked for existing position.")

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
