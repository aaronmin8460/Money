from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    side: str
    current_price: float


@dataclass
class Portfolio:
    cash: float = 100_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    initial_equity: float = 100_000.0
    equity_history: List[float] = field(default_factory=list)
    risk_events: List[Dict[str, str]] = field(default_factory=list)

    def update_position(self, symbol: str, side: str, quantity: float, price: float) -> None:
        if side.upper() == "BUY":
            self.positions[symbol] = Position(symbol, quantity, price, side.upper(), price)
            self.cash -= quantity * price
        elif side.upper() == "SELL" and symbol in self.positions:
            position = self.positions.pop(symbol)
            pnl = (price - position.entry_price) * position.quantity
            self.realized_pnl += pnl
            self.cash += quantity * price

        self._recalculate_equity()

    def mark_to_market(self, prices: Dict[str, float]) -> None:
        total = self.cash
        unrealized = 0.0
        for symbol, position in self.positions.items():
            current_price = prices.get(symbol, position.current_price)
            position.current_price = current_price
            value = position.quantity * current_price
            total += value
            unrealized += (current_price - position.entry_price) * position.quantity

        self.unrealized_pnl = unrealized
        self.equity_history.append(total)

    def _recalculate_equity(self) -> None:
        self.equity_history.append(self.calculate_equity())

    def calculate_equity(self) -> float:
        total = self.cash
        for position in self.positions.values():
            total += position.quantity * position.current_price
        return total

    def exposure(self) -> float:
        return sum(position.quantity * position.current_price for position in self.positions.values())

    def drawdown_pct(self) -> float:
        if not self.equity_history:
            return 0.0
        peak = max(self.equity_history)
        trough = min(self.equity_history)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - trough) / peak)

    def daily_loss_pct(self) -> float:
        if not self.equity_history:
            return 0.0
        current = self.equity_history[-1]
        return max(0.0, (self.initial_equity - current) / self.initial_equity)
