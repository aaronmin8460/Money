from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    initial_quantity: float | None = None
    highest_price_since_entry: float | None = None
    current_stop: float | None = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    entry_signal_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.quantity = float(self.quantity)
        self.entry_price = float(self.entry_price)
        self.current_price = float(self.current_price)
        if self.initial_quantity is None:
            self.initial_quantity = float(self.quantity)
        if self.highest_price_since_entry is None:
            self.highest_price_since_entry = max(self.entry_price, self.current_price)
        if self.current_stop is None:
            stop_price = self.entry_signal_metadata.get("stop_price")
            self.current_stop = float(stop_price) if stop_price is not None else None


@dataclass
class Portfolio:
    cash: float = 100_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    initial_equity: float = 100_000.0
    equity_history: List[float] = field(default_factory=list)
    daily_baseline_equity: float | None = None
    daily_baseline_date: date | None = None
    risk_events: List[Dict[str, Any]] = field(default_factory=list)
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
                "initial_quantity": position.initial_quantity,
                "highest_price_since_entry": position.highest_price_since_entry,
                "current_stop": position.current_stop,
                "tp1_hit": position.tp1_hit,
                "tp2_hit": position.tp2_hit,
                "entry_signal_metadata": dict(position.entry_signal_metadata),
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
        *,
        order_intent: str | None = None,
        reduce_only: bool = False,
        exit_stage: str | None = None,
        signal_metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"} and order_intent is None:
            self._recalculate_equity()
            return
        resolved_intent = order_intent or ("long_entry" if normalized_side == "BUY" else "long_exit")
        metadata = dict(signal_metadata or {})

        if resolved_intent == "long_entry":
            self._apply_long_entry(
                symbol=symbol,
                quantity=quantity,
                price=price,
                asset_class=asset_class,
                exchange=exchange,
                signal_metadata=metadata,
            )
        elif resolved_intent == "long_exit":
            self._apply_long_exit(
                symbol=symbol,
                quantity=quantity,
                price=price,
                reduce_only=reduce_only,
                exit_stage=exit_stage,
                signal_metadata=metadata,
            )

        self._recalculate_equity()

    def _apply_long_entry(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        asset_class: AssetClass,
        exchange: str | None,
        signal_metadata: dict[str, Any],
    ) -> None:
        existing = self.positions.get(symbol)
        if existing:
            total_quantity = existing.quantity + quantity
            average_entry = (
                (existing.entry_price * existing.quantity) + (price * quantity)
            ) / total_quantity
            merged_metadata = {
                **existing.entry_signal_metadata,
                **{key: value for key, value in signal_metadata.items() if value is not None},
            }
            existing.quantity = total_quantity
            existing.entry_price = average_entry
            existing.current_price = price
            existing.side = "BUY"
            existing.asset_class = asset_class
            existing.exchange = exchange
            existing.initial_quantity = max(existing.initial_quantity or 0.0, total_quantity)
            existing.entry_signal_metadata = merged_metadata
            self.record_new_high(symbol, price)
            stop_price = merged_metadata.get("stop_price")
            if stop_price is not None:
                existing.current_stop = max(existing.current_stop or float(stop_price), float(stop_price))
        else:
            self.positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                entry_price=price,
                side="BUY",
                current_price=price,
                asset_class=asset_class,
                exchange=exchange,
                initial_quantity=quantity,
                highest_price_since_entry=price,
                current_stop=signal_metadata.get("stop_price"),
                entry_signal_metadata=signal_metadata,
            )
        self.cash -= quantity * price
        self.last_trade_time = datetime.utcnow()

    def _apply_long_exit(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float,
        reduce_only: bool,
        exit_stage: str | None,
        signal_metadata: dict[str, Any],
    ) -> None:
        position = self.positions.get(symbol)
        if position is None:
            return

        if reduce_only and not self.is_sellable_long_position(symbol):
            return

        sold_quantity = min(quantity, position.quantity)
        if sold_quantity <= 0:
            return

        pnl = (price - position.entry_price) * sold_quantity
        self.realized_pnl += pnl
        self.cash += sold_quantity * price
        remaining_quantity = position.quantity - sold_quantity
        self.last_trade_time = datetime.utcnow()

        self.update_stop_target_state(
            symbol,
            current_stop=signal_metadata.get("next_stop", signal_metadata.get("current_stop")),
            tp1_hit=True if exit_stage == "tp1" else None,
            tp2_hit=True if exit_stage == "tp2" else None,
            metadata_updates=signal_metadata,
        )

        if remaining_quantity <= 0:
            self.positions.pop(symbol, None)
            return

        position.quantity = remaining_quantity
        position.current_price = price

    def record_new_high(self, symbol: str, price: float) -> float | None:
        position = self.positions.get(symbol)
        if position is None:
            return None
        return self._record_position_high(position, price)

    def update_stop_target_state(
        self,
        symbol: str,
        *,
        current_stop: float | None = None,
        tp1_hit: bool | None = None,
        tp2_hit: bool | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Position | None:
        position = self.positions.get(symbol)
        if position is None:
            return None
        if current_stop is not None:
            position.current_stop = float(current_stop)
        if tp1_hit is not None:
            position.tp1_hit = tp1_hit
        if tp2_hit is not None:
            position.tp2_hit = tp2_hit
        if metadata_updates:
            position.entry_signal_metadata = {
                **position.entry_signal_metadata,
                **{key: value for key, value in metadata_updates.items() if value is not None},
            }
        return position

    def get_position_state(self, symbol: str) -> dict[str, Any]:
        position = self.get_position(symbol)
        return {
            "has_tracked_position": position is not None,
            "has_sellable_long_position": self.is_sellable_long_position(symbol),
            "highest_price_since_entry": position.highest_price_since_entry if position is not None else None,
            "current_stop": position.current_stop if position is not None else None,
            "tp1_hit": position.tp1_hit if position is not None else False,
            "tp2_hit": position.tp2_hit if position is not None else False,
            "initial_quantity": position.initial_quantity if position is not None else None,
            "entry_signal_metadata": dict(position.entry_signal_metadata) if position is not None else {},
        }

    def mark_to_market(self, prices: Dict[str, float]) -> None:
        total = self.cash
        unrealized = 0.0
        for symbol, position in self.positions.items():
            current_price = prices.get(symbol, position.current_price)
            position.current_price = current_price
            self._record_position_high(position, current_price)
            value = position.quantity * current_price
            total += value
            unrealized += (current_price - position.entry_price) * position.quantity

        self.unrealized_pnl = unrealized
        self._record_equity_snapshot(total)

    def sync_account_state(self, cash: float, equity: float | None = None) -> None:
        self.cash = cash
        current_equity = equity if equity is not None else self.calculate_equity()
        self._record_equity_snapshot(current_equity)

    def _recalculate_equity(self) -> None:
        self._record_equity_snapshot(self.calculate_equity())

    def _record_position_high(self, position: Position, price: float) -> float:
        latest_price = float(price)
        position.highest_price_since_entry = max(
            position.highest_price_since_entry or position.entry_price,
            latest_price,
        )
        return position.highest_price_since_entry

    def calculate_equity(self) -> float:
        total = self.cash
        for position in self.positions.values():
            total += position.quantity * position.current_price
        return total

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def is_long_position(self, symbol: str) -> bool:
        position = self.get_position(symbol)
        if position is None or position.quantity <= 0:
            return False
        return str(position.side).upper() not in {"SELL", "SHORT"}

    def is_sellable_long_position(self, symbol: str) -> bool:
        return self.is_long_position(symbol)

    def positions_diagnostics(self) -> List[Dict[str, Any]]:
        diagnostics: List[Dict[str, Any]] = []
        for position in self.positions.values():
            is_long = self.is_long_position(position.symbol)
            diagnostics.append(
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "side": position.side,
                    "asset_class": position.asset_class.value,
                    "exchange": position.exchange,
                    "market_value": position.quantity * position.current_price,
                    "initial_quantity": position.initial_quantity,
                    "highest_price_since_entry": position.highest_price_since_entry,
                    "current_stop": position.current_stop,
                    "tp1_hit": position.tp1_hit,
                    "tp2_hit": position.tp2_hit,
                    "is_long": is_long,
                    "sellable": is_long and position.quantity > 0,
                }
            )
        return diagnostics

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
        return self.current_daily_loss_pct()

    def reset_daily_baseline(
        self,
        equity: float | None = None,
        *,
        as_of: datetime | None = None,
    ) -> float:
        baseline_time = self._normalize_timestamp(as_of)
        baseline_equity = float(equity if equity is not None else self.calculate_equity())
        self.daily_baseline_date = baseline_time.date()
        self.daily_baseline_equity = baseline_equity
        return baseline_equity

    def maybe_reset_daily_baseline(
        self,
        equity: float | None = None,
        *,
        as_of: datetime | None = None,
    ) -> bool:
        baseline_time = self._normalize_timestamp(as_of)
        baseline_date = baseline_time.date()
        if self.daily_baseline_equity is None or self.daily_baseline_date is None:
            self.reset_daily_baseline(equity=equity, as_of=baseline_time)
            return True
        if baseline_date > self.daily_baseline_date:
            self.reset_daily_baseline(equity=equity, as_of=baseline_time)
            return True
        return False

    def current_daily_loss_amount(
        self,
        *,
        as_of: datetime | None = None,
        equity: float | None = None,
    ) -> float:
        current_equity = float(equity if equity is not None else self.calculate_equity())
        self.maybe_reset_daily_baseline(equity=current_equity, as_of=as_of)
        baseline_equity = self.daily_baseline_equity if self.daily_baseline_equity is not None else current_equity
        return max(0.0, baseline_equity - current_equity)

    def current_daily_loss_pct(
        self,
        *,
        as_of: datetime | None = None,
        equity: float | None = None,
    ) -> float:
        current_equity = float(equity if equity is not None else self.calculate_equity())
        self.maybe_reset_daily_baseline(equity=current_equity, as_of=as_of)
        baseline_equity = self.daily_baseline_equity if self.daily_baseline_equity is not None else current_equity
        if baseline_equity <= 0:
            return 0.0
        return self.current_daily_loss_amount(as_of=as_of, equity=current_equity) / baseline_equity

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
                existing = self.positions.get(symbol)
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
                    initial_quantity=max(existing.initial_quantity or 0.0, qty) if existing is not None else qty,
                    highest_price_since_entry=(
                        max(existing.highest_price_since_entry or 0.0, current_price, entry_price)
                        if existing is not None
                        else max(current_price, entry_price)
                    ),
                    current_stop=existing.current_stop if existing is not None else None,
                    tp1_hit=existing.tp1_hit if existing is not None else False,
                    tp2_hit=existing.tp2_hit if existing is not None else False,
                    entry_signal_metadata=dict(existing.entry_signal_metadata) if existing is not None else {},
                )

        self._recalculate_equity()

    def _record_equity_snapshot(self, equity: float) -> None:
        snapshot_equity = float(equity)
        if not self.equity_history:
            self.initial_equity = snapshot_equity
        self.equity_history.append(snapshot_equity)
        self.maybe_reset_daily_baseline(equity=snapshot_equity)

    def _normalize_timestamp(self, value: datetime | None) -> datetime:
        resolved = value or datetime.now(timezone.utc)
        if resolved.tzinfo is None:
            return resolved.replace(tzinfo=timezone.utc)
        return resolved.astimezone(timezone.utc)

    def reset_runtime_state(
        self,
        *,
        cash: float | None = None,
        equity: float | None = None,
        reset_daily_baseline_to_current_equity: bool = False,
    ) -> None:
        self.positions.clear()
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.equity_history.clear()
        self.daily_baseline_equity = None
        self.daily_baseline_date = None
        self.risk_events.clear()
        self.last_trade_time = None

        if cash is not None:
            self.cash = float(cash)

        current_equity = float(
            equity
            if equity is not None
            else (cash if cash is not None else self.cash)
        )
        self.initial_equity = current_equity

        if reset_daily_baseline_to_current_equity:
            self.daily_baseline_equity = current_equity
            self.daily_baseline_date = self._normalize_timestamp(None).date()
            self.equity_history.append(current_equity)
