from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from app.domain.models import AssetClass


@dataclass
class Position:
    symbol: str
    quantity: float
    entry_price: float
    side: str
    current_price: float
    asset_class: AssetClass = AssetClass.EQUITY
    exchange: str | None = None


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

    def positions_snapshot(self) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": position.symbol,
                "quantity": position.quantity,
                "qty": position.quantity,
                "entry_price": position.entry_price,
                "avg_entry_price": position.entry_price,
                "side": position.side,
                "current_price": position.current_price,
                "asset_class": position.asset_class.value,
                "exchange": position.exchange,
                "market_value": position.quantity * position.current_price,
            }
            for position in self.positions.values()
        ]

    def update_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        asset_class: AssetClass = AssetClass.EQUITY,
        exchange: str | None = None,
    ) -> None:
        if side.upper() == "BUY":
            existing = self.positions.get(symbol)
            if existing:
                total_quantity = existing.quantity + quantity
                average_entry = (
                    (existing.entry_price * existing.quantity) + (price * quantity)
                ) / total_quantity
                self.positions[symbol] = Position(
                    symbol,
                    total_quantity,
                    average_entry,
                    side.upper(),
                    price,
                    asset_class=asset_class,
                    exchange=exchange,
                )
            else:
                self.positions[symbol] = Position(
                    symbol,
                    quantity,
                    price,
                    side.upper(),
                    price,
                    asset_class=asset_class,
                    exchange=exchange,
                )
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

    def sync_account_state(self, cash: float, equity: float | None = None) -> None:
        self.cash = cash
        current_equity = equity if equity is not None else self.calculate_equity()
        if not self.equity_history:
            self.initial_equity = current_equity
        self.equity_history.append(current_equity)

    def _recalculate_equity(self) -> None:
        self.equity_history.append(self.calculate_equity())

    def calculate_equity(self) -> float:
        total = self.cash
        for position in self.positions.values():
            total += position.quantity * position.current_price
        return total

    def exposure(self) -> float:
        return sum(position.quantity * position.current_price for position in self.positions.values())

    def exposure_by_asset_class(self) -> Dict[str, float]:
        exposure: Dict[str, float] = {}
        for position in self.positions.values():
            key = position.asset_class.value
            exposure[key] = exposure.get(key, 0.0) + (position.quantity * position.current_price)
        return exposure

    def position_counts_by_asset_class(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for position in self.positions.values():
            key = position.asset_class.value
            counts[key] = counts.get(key, 0) + 1
        return counts

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
            qty = float(pos.get("qty", pos.get("quantity", 0)))
            if qty == 0:
                self.positions.pop(symbol, None)
            else:
                entry_price = float(pos.get("avg_entry_price", pos.get("entry_price", 0)))
                current_price = float(
                    pos.get("current_price", pos.get("last_price", pos.get("price", entry_price)))
                )
                side = pos.get("side", "long")
                asset_class = pos.get("asset_class", AssetClass.EQUITY.value)
                try:
                    normalized_asset_class = AssetClass(str(asset_class))
                except ValueError:
                    normalized_asset_class = AssetClass.EQUITY
                self.positions[symbol] = Position(
                    symbol,
                    qty,
                    entry_price,
                    side,
                    current_price,
                    asset_class=normalized_asset_class,
                    exchange=pos.get("exchange"),
                )

        self._recalculate_equity()
