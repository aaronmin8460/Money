from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock
from typing import Any

from app.domain.models import AssetClass

MONEY_QUANTUM = Decimal("0.01")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    resolved = value
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class TranchePlanState:
    symbol: str
    asset_class: AssetClass
    target_position_notional: float
    tranche_count_total: int
    tranche_weights: list[float]
    filled_tranche_count: int = 0
    filled_tranche_notionals: list[float] = field(default_factory=list)
    filled_notional_total: float = 0.0
    remaining_notional: float = 0.0
    average_entry_price: float | None = None
    last_fill_price: float | None = None
    last_tranche_fill_time: datetime | None = None
    last_tranche_fill_bar_index: int | None = None
    scale_in_mode: str = "confirmation"
    allow_average_down: bool = False
    last_decision_reason: str | None = None
    blocked_reason: str | None = None
    plan_status: str = "active"
    filled_quantity_total: float = 0.0

    def __post_init__(self) -> None:
        if self.remaining_notional <= 0:
            self.remaining_notional = max(0.0, self.target_position_notional - self.filled_notional_total)


class TrancheStateStore:
    def __init__(self) -> None:
        self._plans: dict[str, TranchePlanState] = {}
        self._scan_bar_index: int = 0
        self._lock = RLock()

    def increment_scan_bar_index(self) -> int:
        with self._lock:
            self._scan_bar_index += 1
            return self._scan_bar_index

    def get_scan_bar_index(self) -> int:
        with self._lock:
            return self._scan_bar_index

    def get_plan(self, symbol: str) -> TranchePlanState | None:
        with self._lock:
            return self._plans.get(symbol.strip().upper())

    def upsert_plan(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        target_position_notional: float,
        tranche_weights: list[float],
        scale_in_mode: str,
        allow_average_down: bool,
        decision_reason: str | None = None,
    ) -> TranchePlanState:
        normalized_symbol = symbol.strip().upper()
        with self._lock:
            existing = self._plans.get(normalized_symbol)
            if existing is not None:
                existing.target_position_notional = float(target_position_notional)
                existing.tranche_count_total = len(tranche_weights)
                existing.tranche_weights = list(tranche_weights)
                existing.scale_in_mode = scale_in_mode
                existing.allow_average_down = allow_average_down
                existing.plan_status = "active" if existing.remaining_notional > 0 else existing.plan_status
                if decision_reason:
                    existing.last_decision_reason = decision_reason
                return existing

            state = TranchePlanState(
                symbol=normalized_symbol,
                asset_class=asset_class,
                target_position_notional=float(target_position_notional),
                tranche_count_total=len(tranche_weights),
                tranche_weights=list(tranche_weights),
                remaining_notional=float(target_position_notional),
                scale_in_mode=scale_in_mode,
                allow_average_down=allow_average_down,
                last_decision_reason=decision_reason,
            )
            self._plans[normalized_symbol] = state
            return state

    def clear_symbol(self, symbol: str) -> None:
        with self._lock:
            self._plans.pop(symbol.strip().upper(), None)

    def clear_all(self) -> None:
        with self._lock:
            self._plans.clear()
            self._scan_bar_index = 0

    def mark_decision(
        self,
        symbol: str,
        *,
        reason: str | None = None,
        blocked_reason: str | None = None,
        plan_status: str | None = None,
    ) -> None:
        with self._lock:
            plan = self._plans.get(symbol.strip().upper())
            if plan is None:
                return
            if reason:
                plan.last_decision_reason = reason
            if blocked_reason is not None:
                plan.blocked_reason = blocked_reason
            if plan_status:
                plan.plan_status = plan_status

    def compute_next_tranche(
        self,
        symbol: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            plan = self._plans.get(symbol.strip().upper())
            if plan is None:
                return None
            return self._next_tranche_from_plan(plan)

    def record_fill(
        self,
        *,
        symbol: str,
        filled_notional: float,
        fill_price: float,
        fill_time: datetime | None = None,
        bar_index: int | None = None,
        reason: str | None = None,
    ) -> TranchePlanState | None:
        with self._lock:
            normalized_symbol = symbol.strip().upper()
            plan = self._plans.get(normalized_symbol)
            if plan is None:
                return None

            fill_notional_decimal = _round_money(_decimal(filled_notional))
            fill_price_decimal = _decimal(fill_price)
            fill_qty_decimal = Decimal("0")
            if fill_price_decimal > 0:
                fill_qty_decimal = fill_notional_decimal / fill_price_decimal

            plan.filled_tranche_notionals.append(float(fill_notional_decimal))
            plan.filled_tranche_count = len(plan.filled_tranche_notionals)
            plan.filled_notional_total = float(
                _round_money(sum((_decimal(item) for item in plan.filled_tranche_notionals), Decimal("0")))
            )
            plan.remaining_notional = float(
                max(
                    Decimal("0"),
                    _round_money(_decimal(plan.target_position_notional)) - _decimal(plan.filled_notional_total),
                )
            )
            plan.filled_quantity_total = float(_decimal(plan.filled_quantity_total) + fill_qty_decimal)
            if plan.filled_quantity_total > 0:
                plan.average_entry_price = float(
                    _round_money(_decimal(plan.filled_notional_total) / _decimal(plan.filled_quantity_total))
                )
            plan.last_fill_price = float(fill_price)
            plan.last_tranche_fill_time = fill_time or datetime.now(timezone.utc)
            plan.last_tranche_fill_bar_index = bar_index if bar_index is not None else self._scan_bar_index
            plan.blocked_reason = None
            if reason:
                plan.last_decision_reason = reason

            if plan.remaining_notional <= 0.0 or plan.filled_tranche_count >= plan.tranche_count_total:
                plan.plan_status = "completed"
                plan.remaining_notional = 0.0
            else:
                plan.plan_status = "active"
            return plan

    def mark_position_closed(self, symbol: str, *, reason: str | None = None) -> None:
        with self._lock:
            plan = self._plans.get(symbol.strip().upper())
            if plan is None:
                return
            plan.plan_status = "closed"
            plan.remaining_notional = 0.0
            if reason:
                plan.last_decision_reason = reason

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._plan_to_dict(plan) for plan in self._plans.values()]

    def get_plan_snapshot(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            plan = self._plans.get(symbol.strip().upper())
            if plan is None:
                return None
            return self._plan_to_dict(plan)

    def _next_tranche_from_plan(self, plan: TranchePlanState) -> dict[str, Any]:
        next_index = plan.filled_tranche_count
        if next_index >= plan.tranche_count_total or plan.remaining_notional <= 0:
            return {
                "next_tranche_number": None,
                "next_tranche_notional": 0.0,
                "remaining_allocation": float(plan.remaining_notional),
            }

        target_decimal = _round_money(_decimal(plan.target_position_notional))
        remaining_decimal = _round_money(_decimal(plan.remaining_notional))
        tranche_weight = _decimal(plan.tranche_weights[next_index])
        tranche_notional = _round_money(target_decimal * tranche_weight)
        if next_index == plan.tranche_count_total - 1 or tranche_notional > remaining_decimal:
            tranche_notional = remaining_decimal
        return {
            "next_tranche_number": next_index + 1,
            "next_tranche_notional": float(max(Decimal("0"), tranche_notional)),
            "remaining_allocation": float(remaining_decimal),
        }

    def _plan_to_dict(self, plan: TranchePlanState) -> dict[str, Any]:
        next_tranche = self._next_tranche_from_plan(plan)
        return {
            "symbol": plan.symbol,
            "asset_class": plan.asset_class.value,
            "target_position_notional": plan.target_position_notional,
            "tranche_count_total": plan.tranche_count_total,
            "tranche_weights": list(plan.tranche_weights),
            "filled_tranche_count": plan.filled_tranche_count,
            "filled_tranche_notionals": list(plan.filled_tranche_notionals),
            "filled_notional_total": plan.filled_notional_total,
            "current_filled_notional": plan.filled_notional_total,
            "remaining_notional": plan.remaining_notional,
            "remaining_allocation": next_tranche["remaining_allocation"],
            "next_tranche_number": next_tranche["next_tranche_number"],
            "next_tranche_notional": next_tranche["next_tranche_notional"],
            "average_entry_price": plan.average_entry_price,
            "last_fill_price": plan.last_fill_price,
            "last_tranche_fill_time": _iso(plan.last_tranche_fill_time),
            "last_tranche_fill_bar_index": plan.last_tranche_fill_bar_index,
            "scale_in_mode": plan.scale_in_mode,
            "allow_average_down": plan.allow_average_down,
            "last_decision_reason": plan.last_decision_reason,
            "blocked_reason": plan.blocked_reason,
            "plan_status": plan.plan_status,
        }
