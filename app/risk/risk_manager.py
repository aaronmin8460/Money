from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.config.settings import get_settings, Settings
from app.portfolio.portfolio import Portfolio


@dataclass
class RiskDecision:
    approved: bool
    reason: str


class RiskManager:
    def __init__(
        self,
        portfolio: "Portfolio",
        settings: Settings | None = None,
        broker: Any | None = None,
    ):
        self.settings = settings or get_settings()
        self.portfolio = portfolio
        self.broker = broker

    def get_account_snapshot(self) -> dict[str, float]:
        if self.broker is not None:
            try:
                account = self.broker.get_account()
                return {
                    "cash": float(account.cash),
                    "equity": float(account.equity),
                    "buying_power": float(account.buying_power),
                }
            except Exception:
                pass

        equity = self.portfolio.calculate_equity()
        return {
            "cash": float(self.portfolio.cash),
            "equity": float(equity),
            "buying_power": float(self.portfolio.cash),
        }

    def get_runtime_snapshot(self) -> dict[str, Any]:
        account = self.get_account_snapshot()
        return {
            "trading_enabled": self.settings.trading_enabled,
            "broker_mode": self.settings.broker_mode,
            "cash": account["cash"],
            "equity": account["equity"],
            "buying_power": account["buying_power"],
            "open_positions_count": len(self.portfolio.positions),
            "risk_events": list(self.portfolio.risk_events),
            "drawdown_pct": self.portfolio.drawdown_pct(),
            "daily_loss_pct": self.portfolio.daily_loss_pct(),
        }

    def evaluate_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_price: float | None = None,
    ) -> RiskDecision:
        normalized_side = side.value if hasattr(side, "value") else str(side)
        normalized_side = normalized_side.upper()

        if not self.settings.trading_enabled:
            return RiskDecision(True, "Trading is disabled. The order will be evaluated as a dry-run.")

        if quantity <= 0 or price <= 0:
            return RiskDecision(False, "Invalid order quantity or price.")

        if self.portfolio.drawdown_pct() >= self.settings.max_drawdown_pct:
            return RiskDecision(False, f"Max drawdown ({self.portfolio.drawdown_pct():.2%}) exceeded ({self.settings.max_drawdown_pct:.2%}).")

        if self.portfolio.daily_loss_pct() >= self.settings.max_daily_loss_pct:
            return RiskDecision(False, f"Max daily loss ({self.portfolio.daily_loss_pct():.2%}) reached ({self.settings.max_daily_loss_pct:.2%}).")

        account = self.get_account_snapshot()

        if normalized_side == "BUY":
            if symbol in self.portfolio.positions:
                return RiskDecision(False, "Duplicate buy order blocked for existing position.")

            if len(self.portfolio.positions) >= self.settings.max_positions:
                return RiskDecision(False, f"Maximum simultaneous positions ({self.settings.max_positions}) reached.")

            order_notional = quantity * price
            if order_notional > self.settings.max_position_notional:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds max position notional ({self.settings.max_position_notional:.2f}).",
                )

            if self.settings.is_paper_mode and order_notional > account["cash"]:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds available cash ({account['cash']:.2f}).",
                )

            if order_notional > account["buying_power"]:
                return RiskDecision(
                    False,
                    f"Order notional ({order_notional:.2f}) exceeds buying power ({account['buying_power']:.2f}).",
                )

            if stop_price is not None:
                risk_per_share = price - stop_price
                if risk_per_share <= 0:
                    return RiskDecision(False, "Stop price must be below entry price for a long position.")

                trade_risk = quantity * risk_per_share
                max_trade_risk = account["equity"] * self.settings.max_risk_per_trade
                if trade_risk > max_trade_risk:
                    return RiskDecision(
                        False,
                        f"Stop-based trade risk ({trade_risk:.2f}) exceeds max risk per trade ({max_trade_risk:.2f}).",
                    )

        if self.settings.is_alpaca_mode and not self.settings.allow_extended_hours:
            broker = self.broker
            if broker is not None and hasattr(broker, "is_market_open") and not broker.is_market_open():
                return RiskDecision(False, "Market is closed and extended hours not allowed.")

        return RiskDecision(True, "Order approved by risk manager.")

    def record_event(self, symbol: Optional[str], reason: str, details: Optional[str] = None) -> None:
        self.portfolio.risk_events.append({
            "symbol": symbol,
            "reason": reason,
            "details": details,
        })

    def guard_against(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_price: float | None = None,
    ) -> RiskDecision:
        decision = self.evaluate_order(symbol, side, quantity, price, stop_price=stop_price)
        if not decision.approved:
            self.record_event(
                symbol,
                decision.reason,
                f"side={side}, qty={quantity}, price={price}, stop_price={stop_price}",
            )
        return decision
