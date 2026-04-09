from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


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
    last_trade_time: datetime | None = None

    def update_position(self, symbol: str, side: str, quantity: float, price: float) -> None:
        if side.upper() == "BUY":
            self.positions[symbol] = Position(symbol, quantity, price, side.upper(), price)
            self.cash -= quantity * price
            self.last_trade_time = datetime.utcnow()
        elif side.upper() == "SELL" and symbol in self.positions:
            position = self.positions.pop(symbol)
            pnl = (price - position.entry_price) * position.quantity
            self.realized_pnl += pnl
            self.cash += quantity * price
            self.last_trade_time = datetime.utcnow()

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
        start_equity = self.equity_history[0]
        current_equity = self.calculate_equity()
        if start_equity <= 0:
            return 0.0
        loss = max(0.0, start_equity - current_equity)
        return loss / start_equity

    def reconcile_positions(self, broker_positions: List[Dict[str, Any]]) -> None:
        """Reconcile portfolio with broker positions."""
        broker_symbols = {pos.get("symbol") or pos.get("sym"): pos for pos in broker_positions}
        
        # Remove positions not in broker
        to_remove = []
        for symbol in self.positions:
            if symbol not in broker_symbols:
                to_remove.append(symbol)
        for symbol in to_remove:
            del self.positions[symbol]
        
        # Update existing positions
        for symbol, pos in broker_symbols.items():
            qty = float(pos.get("qty", 0))
            if qty == 0:
                self.positions.pop(symbol, None)
            else:
                entry_price = float(pos.get("avg_entry_price", 0))
                current_price = float(pos.get("current_price", pos.get("last_price", 0)))
                side = pos.get("side", "long")
                self.positions[symbol] = Position(symbol, qty, entry_price, side, current_price)
        
        self._recalculate_equity()
