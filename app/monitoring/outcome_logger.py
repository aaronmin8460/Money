from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.config.settings import Settings, get_settings
from app.ml.features import build_signal_feature_row, resolve_model_purpose
from app.monitoring.events import StructuredEvent, normalize_outcome_classification
from app.monitoring.jsonl_store import JsonlStore
from app.monitoring.logger import get_logger

if TYPE_CHECKING:
    from app.risk.risk_manager import RiskDecision
    from app.services.broker import OrderRequest
    from app.strategies.base import TradeSignal

logger = get_logger("outcome_logger")


class OutcomeLogger:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.outcome_store = JsonlStore(f"{self.settings.log_dir}/outcomes.jsonl")

    def log_execution_outcome(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        risk: RiskDecision | dict[str, Any] | None,
        order: dict[str, Any] | None = None,
        market_overview: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        risk_payload = risk.to_dict() if hasattr(risk, "to_dict") else dict(risk or {})
        classification = normalize_outcome_classification(action, risk_payload.get("rule"))
        feature_row = build_signal_feature_row(
            signal,
            cycle_id=str((signal.metrics or {}).get("cycle_id") or ""),
            outcome_classification=classification,
            latest_price=signal.price or signal.entry_price,
            market_overview=market_overview,
            news_features=news_features,
        )
        label, label_source = derive_bootstrap_label(
            signal=signal,
            action=action,
            classification=classification,
            feature_snapshot=feature_row,
        )
        feature_row.label = label
        feature_row.label_source = label_source
        feature_row.metadata.update(
            {
                "bootstrap_action": action,
                "bootstrap_classification": classification,
                "bootstrap_model_purpose": feature_row.model_purpose,
                "bootstrap_realized_proxy": _first_proxy_value(
                    feature_row.realized_return,
                    feature_row.forward_return,
                    feature_row.risk_adjusted_return,
                ),
                "bootstrap_unrealized_return": _first_proxy_value(
                    feature_row.unrealized_return,
                    ((signal.metrics or {}).get("exit_state") or {}).get("unrealized_return"),
                ),
                "bootstrap_exit_stage": str(feature_row.exit_stage or signal.exit_stage or ""),
            }
        )
        event = StructuredEvent(
            event_type="outcome",
            payload={
                "signal_id": feature_row.signal_id,
                "cycle_id": feature_row.cycle_id,
                "symbol": signal.symbol,
                "asset_class": signal.asset_class.value,
                "strategy_name": signal.strategy_name,
                "signal": signal.signal.value,
                "action": action,
                "classification": classification,
                "risk_rule": risk_payload.get("rule"),
                "risk_reason": risk_payload.get("reason"),
                "order_id": (order or {}).get("id") or (order or {}).get("client_order_id"),
                "feature_snapshot": feature_row.to_dict(),
                "proposal": {
                    "symbol": proposal.symbol,
                    "side": proposal.side.value if hasattr(proposal.side, "value") else str(proposal.side),
                    "quantity": proposal.quantity,
                    "notional": proposal.notional,
                    "price": proposal.price,
                    "is_dry_run": proposal.is_dry_run,
                },
                "order": dict(order or {}),
                "ml": dict((signal.metrics or {}).get("ml") or {}),
                "news": dict(news_features or {}),
            },
        )
        payload = event.to_dict()
        self.outcome_store.append(payload)
        logger.info(
            "Structured outcome log written",
            extra={
                "symbol": signal.symbol,
                "classification": classification,
                "path": str(self.outcome_store.path),
            },
        )
        return payload


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_proxy_value(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _is_authoritative_exit_stage(exit_stage: str | None) -> bool:
    return exit_stage in {"stop", "trail", "break_even_stop", "emergency", "time_stop", "regime_deterioration"}


def _derive_entry_bootstrap_label(*, signal: TradeSignal, action: str, classification: str) -> tuple[int | None, str | None]:
    if classification == "ml_inference_error":
        return None, None
    if classification in {"risk_rejected", "no_position_to_sell"}:
        return 0, "execution_outcome"
    if classification in {"submitted", "dry_run"}:
        entry = signal.entry_price or signal.price
        stop = signal.stop_price
        target = signal.target_price
        if signal.signal.value == "BUY" and entry and stop and target and entry > stop:
            reward_risk = (target - entry) / max(entry - stop, 1e-9)
            return (1 if reward_risk >= 1.0 else 0), "reward_risk_bootstrap"
        return 1, "execution_outcome"
    if action == "hold":
        return None, None
    return 0, "execution_outcome"


def _derive_exit_bootstrap_label(
    *,
    signal: TradeSignal,
    action: str,
    classification: str,
    feature_snapshot: Any | None,
) -> tuple[int | None, str | None]:
    if classification == "ml_inference_error":
        return None, None
    if classification == "dust_resolved":
        return None, None
    if classification in {"risk_rejected", "no_position_to_sell"}:
        return 0, "exit_execution_outcome"

    snapshot = feature_snapshot.to_dict() if hasattr(feature_snapshot, "to_dict") else dict(feature_snapshot or {})
    realized_proxy = _first_proxy_value(
        snapshot.get("realized_return"),
        snapshot.get("forward_return"),
        snapshot.get("risk_adjusted_return"),
    )
    unrealized_return = _first_proxy_value(
        snapshot.get("unrealized_return"),
        ((signal.metrics or {}).get("exit_state") or {}).get("unrealized_return"),
        (signal.metrics or {}).get("unrealized_return"),
    )
    exit_stage = str(snapshot.get("exit_stage") or signal.exit_stage or "").strip() or None

    if action == "hold":
        if realized_proxy is None:
            return None, None
        return (1 if realized_proxy > 0 else 0), "exit_hold_forward_proxy"

    if classification in {"submitted", "dry_run"}:
        if _is_authoritative_exit_stage(exit_stage):
            return 1, "exit_authoritative_bootstrap"
        if realized_proxy is not None:
            return (1 if realized_proxy > 0 else 0), "exit_realized_proxy"
        if exit_stage in {"tp1", "tp2", "tp3", "tp4", "ml_exit"} and unrealized_return is not None:
            return (1 if unrealized_return > 0 else 0), "exit_unrealized_proxy"
        return None, None

    if action == "skipped":
        return None, None
    return 0, "exit_execution_outcome"


def derive_bootstrap_label(
    *,
    signal: TradeSignal,
    action: str,
    classification: str,
    feature_snapshot: Any | None = None,
) -> tuple[int | None, str | None]:
    model_purpose = (
        str(getattr(feature_snapshot, "model_purpose", "") or "")
        if feature_snapshot is not None
        else ""
    )
    if not model_purpose and isinstance(feature_snapshot, dict):
        model_purpose = str(feature_snapshot.get("model_purpose") or "")
    if not model_purpose:
        model_purpose = resolve_model_purpose(signal)
    if model_purpose == "exit":
        return _derive_exit_bootstrap_label(
            signal=signal,
            action=action,
            classification=classification,
            feature_snapshot=feature_snapshot,
        )
    return _derive_entry_bootstrap_label(signal=signal, action=action, classification=classification)


_outcome_logger: OutcomeLogger | None = None


def get_outcome_logger(settings: Settings | None = None) -> OutcomeLogger:
    global _outcome_logger
    resolved_settings = settings or get_settings()
    if _outcome_logger is None or _outcome_logger.settings is not resolved_settings:
        _outcome_logger = OutcomeLogger(resolved_settings)
    return _outcome_logger


def reset_outcome_logger() -> None:
    global _outcome_logger
    _outcome_logger = None
