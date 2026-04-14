from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings, get_settings
from app.db.models import Base, RuntimeSafetyState
from app.db.session import SessionLocal, get_engine
from app.domain.models import AssetClass
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio, Position
from app.services.tranche_state import TrancheStateStore
from app.strategies.base import ENTRY_ORDER_INTENTS, EXIT_ORDER_INTENTS

logger = get_logger("runtime_safety")


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _json_loads(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _resolve_asset_class(value: Any) -> str:
    try:
        return AssetClass(str(value)).value
    except ValueError:
        return AssetClass.EQUITY.value


def _resolve_direction(value: Any, *, quantity: float) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"sell", "short"}:
        return "short"
    if normalized in {"buy", "long"}:
        return "long"
    if float(quantity) < 0:
        return "short"
    return "long"


def _normalize_broker_position(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = str(payload.get("symbol") or payload.get("sym") or "").strip().upper()
    quantity = float(payload.get("qty", payload.get("quantity", 0.0)) or 0.0)
    entry_price = float(payload.get("avg_entry_price", payload.get("entry_price", 0.0)) or 0.0)
    current_price = float(payload.get("current_price", payload.get("last_price", payload.get("price", entry_price))) or 0.0)
    return {
        "symbol": symbol,
        "quantity": abs(quantity),
        "direction": _resolve_direction(payload.get("position_direction") or payload.get("side"), quantity=quantity),
        "asset_class": _resolve_asset_class(payload.get("asset_class", AssetClass.EQUITY.value)),
        "exchange": payload.get("exchange"),
        "entry_price": entry_price,
        "current_price": current_price,
    }


def _normalize_local_position(position: Position) -> dict[str, Any]:
    return {
        "symbol": position.symbol.strip().upper(),
        "quantity": abs(float(position.quantity)),
        "direction": position.direction.value,
        "asset_class": position.asset_class.value,
        "exchange": position.exchange,
        "entry_price": float(position.entry_price),
        "current_price": float(position.current_price),
    }


class RuntimeSafetyManager:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        broker: Any,
        portfolio: Portfolio,
        tranche_state: TrancheStateStore,
        risk_manager: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.broker = broker
        self.portfolio = portfolio
        self.tranche_state = tranche_state
        self.risk_manager = risk_manager
        self._lock = threading.RLock()
        Base.metadata.create_all(bind=get_engine(self.settings), checkfirst=True)
        self._ensure_state_row()

    def entries_allowed(self) -> bool:
        return not self.get_state_snapshot()["halted"]

    def get_state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                snapshot = self._serialize_row(row)
        summary = snapshot["last_reconcile_summary"]
        snapshot["new_entries_allowed"] = not snapshot["halted"]
        snapshot["mismatch_summary"] = dict(summary.get("mismatch_counts_by_type") or {})
        snapshot["mismatch_count"] = int(summary.get("mismatch_count") or 0)
        snapshot["material_mismatch_count"] = int(summary.get("material_mismatch_count") or 0)
        return snapshot

    def get_reconciliation_snapshot(self) -> dict[str, Any]:
        snapshot = self.get_state_snapshot()
        summary = dict(snapshot["last_reconcile_summary"])
        return {
            "last_reconcile_status": snapshot["last_reconcile_status"],
            "last_reconcile_summary": summary,
            "mismatch_summary": dict(summary.get("mismatch_counts_by_type") or {}),
            "mismatches": list(summary.get("mismatches") or []),
        }

    def get_runtime_diagnostics(
        self,
        *,
        lock_metadata: dict[str, Any] | None = None,
        loop_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.get_state_snapshot()
        diagnostics = {
            **snapshot,
            "process_lock_metadata": lock_metadata or snapshot["lock_metadata"],
            "loop_safety": dict(loop_metadata or {}),
        }
        diagnostics["new_entries_allowed"] = not diagnostics["halted"]
        return diagnostics

    def update_lock_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        normalized_metadata = dict(metadata or {})
        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                current_metadata = _json_loads(row.lock_metadata_json)
                if current_metadata != normalized_metadata:
                    row.lock_metadata_json = _json_dumps(normalized_metadata)
                    row.updated_at = _utcnow()
                    session.commit()
                snapshot = self._serialize_row(row)
        return snapshot

    def manual_halt(self, *, operator_note: str | None = None) -> dict[str, Any]:
        return self.halt(
            halt_reason="manual_operator_halt",
            halt_rule="manual_halt",
            details={"operator_note": operator_note} if operator_note else {},
            notification_reason="manual operator halt requested",
        )

    def resume(
        self,
        *,
        operator_note: str | None = None,
        reset_consecutive_losing_exits: bool = True,
    ) -> dict[str, Any]:
        details = {
            "operator_note": operator_note,
            "reset_consecutive_losing_exits": reset_consecutive_losing_exits,
        }
        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                row.halted = False
                row.halt_reason = None
                row.halt_rule = None
                row.resumed_at = _utcnow()
                if reset_consecutive_losing_exits:
                    row.consecutive_losing_exits = 0
                row.updated_at = _utcnow()
                session.commit()
                snapshot = self._serialize_row(row)

        self._record_event("runtime_safety_resumed", details=details)
        notifier = get_discord_notifier(self.settings)
        notifier.send_system_notification(
            event="Bot resumed manually",
            reason="runtime safety halt cleared",
            details=details,
            category="runtime_safety",
        )
        return {
            **snapshot,
            "new_entries_allowed": True,
        }

    def halt(
        self,
        *,
        halt_reason: str,
        halt_rule: str,
        details: dict[str, Any] | None = None,
        notification_reason: str | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        event_details = dict(details or {})
        event_details.update({"halt_reason": halt_reason, "halt_rule": halt_rule})
        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                already_halted = bool(row.halted)
                same_state = already_halted and row.halt_reason == halt_reason and row.halt_rule == halt_rule
                row.halted = True
                row.halt_reason = halt_reason
                row.halt_rule = halt_rule
                if row.halted_at is None or not already_halted:
                    row.halted_at = _utcnow()
                row.updated_at = _utcnow()
                session.commit()
                snapshot = self._serialize_row(row)

        if not same_state:
            self._record_event("runtime_safety_halt", details=event_details)
            if notify:
                notifier = get_discord_notifier(self.settings)
                notifier.send_system_notification(
                    event="Bot halted by circuit breaker",
                    reason=notification_reason or halt_reason,
                    details=event_details,
                    category="runtime_safety",
                )

        snapshot["new_entries_allowed"] = False
        return snapshot

    def record_exit_outcome(
        self,
        *,
        symbol: str,
        order_intent: str | None,
        trade_pnl: float | None,
        exit_stage: str | None = None,
    ) -> dict[str, Any]:
        if order_intent not in EXIT_ORDER_INTENTS or trade_pnl is None:
            return self.get_state_snapshot()

        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                if trade_pnl < 0:
                    row.consecutive_losing_exits += 1
                elif trade_pnl > 0:
                    row.consecutive_losing_exits = 0
                row.updated_at = _utcnow()
                session.commit()
                snapshot = self._serialize_row(row)

        snapshot["new_entries_allowed"] = not snapshot["halted"]
        if (
            trade_pnl < 0
            and self.settings.halt_on_consecutive_losses
            and snapshot["consecutive_losing_exits"] >= self.settings.max_consecutive_losing_exits
        ):
            return self.halt(
                halt_reason="consecutive_losing_exits_threshold_reached",
                halt_rule="consecutive_losing_exits",
                details={
                    "symbol": symbol,
                    "trade_pnl": trade_pnl,
                    "exit_stage": exit_stage,
                    "consecutive_losing_exits": snapshot["consecutive_losing_exits"],
                    "max_consecutive_losing_exits": self.settings.max_consecutive_losing_exits,
                },
                notification_reason="consecutive losing exits threshold reached",
            )
        return snapshot

    def reconcile(self, *, source: str = "runtime_sync") -> dict[str, Any]:
        try:
            broker_positions = [
                _normalize_broker_position(position)
                for position in self.broker.get_positions()
            ]
        except Exception as exc:
            return self.record_sync_failure(source=source, stage="broker_positions", error=exc)

        try:
            return self._reconcile_positions(source=source, broker_positions=broker_positions)
        except Exception as exc:
            logger.warning("Runtime reconciliation failed: %s", exc)
            return self.record_sync_failure(source=source, stage="reconcile", error=exc)

    def record_sync_failure(self, *, source: str, stage: str, error: Exception | str) -> dict[str, Any]:
        error_text = f"{type(error).__name__}: {error}" if isinstance(error, Exception) else str(error)
        summary = {
            "checked_at": _iso(_utcnow()),
            "source": source,
            "stage": stage,
            "status": "error",
            "error": error_text,
            "mismatch_count": 0,
            "material_mismatch_count": 0,
            "auto_healed_count": 0,
            "mismatch_counts_by_type": {},
            "mismatches": [],
        }
        changed = self._persist_reconcile_status(status="error", summary=summary)
        if changed:
            self._record_event("startup_sync_failure" if source == "startup" else "reconcile_sync_error", details=summary)
            get_discord_notifier(self.settings).send_system_notification(
                event="Startup sync failure" if source == "startup" else "Reconcile mismatch detected",
                reason=f"{stage} failed",
                details=summary,
                category="runtime_safety",
            )
        if source == "startup" and self.settings.halt_on_startup_sync_failure:
            return self.halt(
                halt_reason="startup_sync_failure",
                halt_rule="startup_sync_failure",
                details=summary,
                notification_reason="startup sync failure detected",
            )
        return self.get_reconciliation_snapshot()

    def _reconcile_positions(
        self,
        *,
        source: str,
        broker_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        local_positions_before = {
            position.symbol: _normalize_local_position(position)
            for position in self.portfolio.positions.values()
        }
        broker_by_symbol = {position["symbol"]: position for position in broker_positions if position["symbol"]}
        local_by_symbol = {symbol: position for symbol, position in local_positions_before.items() if symbol}
        mismatches: list[dict[str, Any]] = []

        all_symbols = sorted(set(broker_by_symbol) | set(local_by_symbol))
        for symbol in all_symbols:
            broker_position = broker_by_symbol.get(symbol)
            local_position = local_by_symbol.get(symbol)
            if broker_position is not None and local_position is None:
                mismatches.append(
                    self._mismatch(
                        kind="broker_position_missing_locally",
                        symbol=symbol,
                        severity="critical",
                        auto_healed=True,
                        broker_position=broker_position,
                    )
                )
                continue
            if local_position is not None and broker_position is None:
                mismatches.append(
                    self._mismatch(
                        kind="local_position_missing_at_broker",
                        symbol=symbol,
                        severity="warning",
                        auto_healed=True,
                        local_position=local_position,
                    )
                )
                continue
            if broker_position is None or local_position is None:
                continue
            if abs(float(broker_position["quantity"]) - float(local_position["quantity"])) > 1e-9:
                mismatches.append(
                    self._mismatch(
                        kind="quantity_mismatch",
                        symbol=symbol,
                        severity="critical",
                        auto_healed=True,
                        broker_position=broker_position,
                        local_position=local_position,
                    )
                )
            if broker_position["direction"] != local_position["direction"]:
                mismatches.append(
                    self._mismatch(
                        kind="direction_mismatch",
                        symbol=symbol,
                        severity="critical",
                        auto_healed=True,
                        broker_position=broker_position,
                        local_position=local_position,
                    )
                )
            if broker_position["asset_class"] != local_position["asset_class"]:
                mismatches.append(
                    self._mismatch(
                        kind="asset_class_mismatch",
                        symbol=symbol,
                        severity="critical",
                        auto_healed=True,
                        broker_position=broker_position,
                        local_position=local_position,
                    )
                )

        if mismatches:
            raw_positions = [
                {
                    "symbol": position["symbol"],
                    "qty": position["quantity"],
                    "avg_entry_price": position["entry_price"],
                    "current_price": position["current_price"],
                    "side": "SELL" if position["direction"] == "short" else "BUY",
                    "asset_class": position["asset_class"],
                    "exchange": position["exchange"],
                    "position_direction": position["direction"],
                }
                for position in broker_positions
            ]
            self.portfolio.reconcile_positions(raw_positions)

        tranche_mismatches = self._reconcile_tranche_state(
            broker_by_symbol=broker_by_symbol,
            local_by_symbol={
                position.symbol: _normalize_local_position(position)
                for position in self.portfolio.positions.values()
            },
        )
        mismatches.extend(tranche_mismatches)

        mismatch_counts_by_type: dict[str, int] = {}
        auto_healed_count = 0
        material_mismatch_count = 0
        for mismatch in mismatches:
            kind = str(mismatch["kind"])
            mismatch_counts_by_type[kind] = mismatch_counts_by_type.get(kind, 0) + 1
            if mismatch.get("auto_healed"):
                auto_healed_count += 1
            if mismatch.get("severity") == "critical":
                material_mismatch_count += 1

        if material_mismatch_count > 0:
            status = "mismatch_detected"
        elif mismatches and auto_healed_count == len(mismatches):
            status = "auto_healed"
        elif mismatches:
            status = "warning"
        else:
            status = "ok"

        summary = {
            "checked_at": _iso(_utcnow()),
            "source": source,
            "status": status,
            "broker_position_count": len(broker_positions),
            "local_position_count_before": len(local_positions_before),
            "local_position_count_after": len(self.portfolio.positions),
            "tranche_plan_count": len(self.tranche_state.snapshot()),
            "mismatch_count": len(mismatches),
            "material_mismatch_count": material_mismatch_count,
            "auto_healed_count": auto_healed_count,
            "mismatch_counts_by_type": mismatch_counts_by_type,
            "mismatches": mismatches[:50],
        }

        changed = self._persist_reconcile_status(status=status, summary=summary)
        if changed and status in {"warning", "auto_healed", "mismatch_detected"}:
            event_name = "Reconcile auto-heal applied" if status == "auto_healed" else "Reconcile mismatch detected"
            reason = "auto-healed local state drift" if status == "auto_healed" else "runtime state mismatch detected"
            event_key = "reconcile_auto_healed" if status == "auto_healed" else "reconcile_mismatch_detected"
            self._record_event(event_key, details=summary)
            get_discord_notifier(self.settings).send_system_notification(
                event=event_name,
                reason=reason,
                details=summary,
                category="runtime_safety",
            )

        if material_mismatch_count > 0 and self.settings.halt_on_reconcile_mismatch:
            return self.halt(
                halt_reason="reconcile_mismatch_detected",
                halt_rule="reconcile_mismatch",
                details=summary,
                notification_reason="material reconcile mismatch detected",
            )

        return self.get_reconciliation_snapshot()

    def _reconcile_tranche_state(
        self,
        *,
        broker_by_symbol: dict[str, dict[str, Any]],
        local_by_symbol: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        plan_snapshot = {item["symbol"]: item for item in self.tranche_state.snapshot()}

        for symbol, plan in plan_snapshot.items():
            has_position = symbol in broker_by_symbol or symbol in local_by_symbol
            if not has_position and plan.get("plan_status") != "closed":
                self.tranche_state.mark_position_closed(
                    symbol,
                    reason="Runtime safety reconciliation closed stale tranche state.",
                )
                mismatches.append(
                    self._mismatch(
                        kind="tranche_state_without_position",
                        symbol=symbol,
                        severity="warning",
                        auto_healed=True,
                        tranche_plan=plan,
                    )
                )
                continue

            if has_position and plan.get("plan_status") == "closed":
                mismatches.append(
                    self._mismatch(
                        kind="closed_tranche_with_open_position",
                        symbol=symbol,
                        severity="critical",
                        auto_healed=False,
                        tranche_plan=plan,
                        local_position=local_by_symbol.get(symbol),
                        broker_position=broker_by_symbol.get(symbol),
                    )
                )

            position_asset_class = (
                (local_by_symbol.get(symbol) or broker_by_symbol.get(symbol) or {}).get("asset_class")
            )
            if position_asset_class and plan.get("asset_class") != position_asset_class:
                mismatches.append(
                    self._mismatch(
                        kind="tranche_asset_class_mismatch",
                        symbol=symbol,
                        severity="warning",
                        auto_healed=False,
                        tranche_plan=plan,
                        local_position=local_by_symbol.get(symbol),
                        broker_position=broker_by_symbol.get(symbol),
                    )
                )

        for symbol in sorted(set(broker_by_symbol) | set(local_by_symbol)):
            if symbol in plan_snapshot:
                continue
            mismatches.append(
                self._mismatch(
                    kind="position_missing_tranche_plan",
                    symbol=symbol,
                    severity="warning",
                    auto_healed=False,
                    local_position=local_by_symbol.get(symbol),
                    broker_position=broker_by_symbol.get(symbol),
                )
            )
        return mismatches

    def _persist_reconcile_status(self, *, status: str, summary: dict[str, Any]) -> bool:
        with self._lock:
            with SessionLocal() as session:
                row = self._get_state_row(session)
                previous_summary = _json_loads(row.last_reconcile_summary_json)
                previous_status = row.last_reconcile_status or "unknown"
                previous_fingerprint = self._reconcile_fingerprint(previous_status, previous_summary)
                row.last_reconcile_status = status
                row.last_reconcile_summary_json = _json_dumps(summary)
                row.updated_at = _utcnow()
                session.commit()
                new_fingerprint = self._reconcile_fingerprint(status, summary)
        return previous_fingerprint != new_fingerprint

    def _record_event(self, reason: str, *, details: dict[str, Any]) -> None:
        if self.risk_manager is None:
            return
        try:
            self.risk_manager.record_event(None, reason, details)
        except Exception as exc:
            logger.warning("Failed to persist runtime safety event %s: %s", reason, exc)

    def _ensure_state_row(self) -> None:
        with self._lock:
            with SessionLocal() as session:
                row = session.get(RuntimeSafetyState, 1)
                if row is None:
                    session.add(
                        RuntimeSafetyState(
                            id=1,
                            halted=False,
                            consecutive_losing_exits=0,
                            updated_at=_utcnow(),
                        )
                    )
                    session.commit()

    def _get_state_row(self, session: Any) -> RuntimeSafetyState:
        row = session.get(RuntimeSafetyState, 1)
        if row is None:
            row = RuntimeSafetyState(
                id=1,
                halted=False,
                consecutive_losing_exits=0,
                updated_at=_utcnow(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def _serialize_row(self, row: RuntimeSafetyState) -> dict[str, Any]:
        return {
            "halted": bool(row.halted),
            "halt_reason": row.halt_reason,
            "halt_rule": row.halt_rule,
            "halted_at": _iso(row.halted_at),
            "resumed_at": _iso(row.resumed_at),
            "consecutive_losing_exits": int(row.consecutive_losing_exits or 0),
            "last_reconcile_status": row.last_reconcile_status,
            "last_reconcile_summary": _json_loads(row.last_reconcile_summary_json),
            "lock_metadata": _json_loads(row.lock_metadata_json),
            "updated_at": _iso(row.updated_at),
        }

    def _mismatch(
        self,
        *,
        kind: str,
        symbol: str,
        severity: str,
        auto_healed: bool,
        broker_position: dict[str, Any] | None = None,
        local_position: dict[str, Any] | None = None,
        tranche_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "symbol": symbol,
            "severity": severity,
            "auto_healed": auto_healed,
            "broker_position": broker_position,
            "local_position": local_position,
            "tranche_plan": tranche_plan,
        }

    def _reconcile_fingerprint(self, status: str, summary: dict[str, Any]) -> str:
        normalized = {
            "status": status,
            "stage": summary.get("stage"),
            "error": summary.get("error"),
            "mismatch_count": summary.get("mismatch_count", 0),
            "material_mismatch_count": summary.get("material_mismatch_count", 0),
            "auto_healed_count": summary.get("auto_healed_count", 0),
            "mismatch_counts_by_type": summary.get("mismatch_counts_by_type", {}),
            "mismatches": summary.get("mismatches", []),
        }
        return hashlib.sha256(_json_dumps(normalized).encode("utf-8")).hexdigest()

    def blocks_new_exposure(self, *, order_intent: str | None, reduce_only: bool) -> bool:
        if reduce_only or order_intent not in ENTRY_ORDER_INTENTS:
            return False
        return self.get_state_snapshot()["halted"]
