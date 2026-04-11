from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.config.settings import Settings, get_settings
from app.ml.features import build_signal_feature_row
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
        label, label_source = derive_bootstrap_label(signal=signal, action=action, classification=classification)
        feature_row = build_signal_feature_row(
            signal,
            cycle_id=str((signal.metrics or {}).get("cycle_id") or ""),
            outcome_classification=classification,
            latest_price=signal.price or signal.entry_price,
            market_overview=market_overview,
            news_features=news_features,
            label=label,
            label_source=label_source,
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


def derive_bootstrap_label(*, signal: TradeSignal, action: str, classification: str) -> tuple[int | None, str | None]:
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
