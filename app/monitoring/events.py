from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_signal_id(symbol: str, strategy_name: str, generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now(timezone.utc)).isoformat()
    raw = f"{symbol.upper()}|{strategy_name}|{stamp}"
    return sha1(raw.encode("utf-8")).hexdigest()


def normalize_outcome_classification(action: str, risk_rule: str | None = None) -> str:
    normalized_action = str(action or "").strip().lower()
    normalized_rule = str(risk_rule or "").strip().lower()

    if normalized_action == "submitted":
        return "submitted"
    if normalized_action == "dry_run":
        return "dry_run"
    if normalized_action == "skipped_low_ml_score":
        return "skipped_low_ml_score"
    if normalized_action in {"dust_resolved", "dust_closed"} or normalized_rule in {"dust_resolved", "dust_closed"}:
        return "dust_resolved"
    if normalized_rule == "ml_inference_error":
        return "ml_inference_error"
    if normalized_rule == "market_closed_extended_hours_disabled":
        return "market_closed_extended_hours_disabled"
    if normalized_rule in {"market_closed", "market_closed_no_extended_session"}:
        return "market_closed"
    if normalized_rule in {"extended_hours_not_eligible", "extended_hours_not_supported_for_asset"}:
        return "extended_hours_not_supported_for_asset"
    if normalized_rule == "no_position_to_sell":
        return "no_position_to_sell"
    if normalized_rule in {"exit_qty_rounds_to_zero", "non_dust_exit_unexecutable"}:
        return normalized_rule
    if normalized_action == "rejected":
        return "risk_rejected"
    if normalized_action == "skipped":
        return normalized_rule or "skipped"
    if normalized_action == "hold":
        return "hold"
    return normalized_action or normalized_rule or "unknown"


@dataclass(frozen=True)
class StructuredEvent:
    event_type: str
    payload: dict[str, Any]
    recorded_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "recorded_at": self.recorded_at,
            **self.payload,
        }
