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

logger = get_logger("trade_logger")


class TradeLogger:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.signal_store = JsonlStore(f"{self.settings.log_dir}/signals.jsonl")
        self.order_store = JsonlStore(f"{self.settings.log_dir}/orders.jsonl")

    def log_signal(
        self,
        signal: TradeSignal,
        *,
        latest_price: float | None = None,
        outcome_classification: str | None = None,
        market_overview: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        feature_row = build_signal_feature_row(
            signal,
            cycle_id=str((signal.metrics or {}).get("cycle_id") or ""),
            outcome_classification=outcome_classification,
            latest_price=latest_price,
            market_overview=market_overview,
            news_features=news_features,
        )
        event = StructuredEvent(
            event_type="signal",
            payload={
                "signal_id": feature_row.signal_id,
                "cycle_id": feature_row.cycle_id,
                "symbol": signal.symbol,
                "asset_class": signal.asset_class.value,
                "strategy_name": signal.strategy_name,
                "signal": signal.signal.value,
                "reason": signal.reason,
                "decision_code": (signal.metrics or {}).get("decision_code"),
                "feature_snapshot": feature_row.to_dict(),
                "metrics": dict(signal.metrics or {}),
            },
        )
        payload = event.to_dict()
        self.signal_store.append(payload)
        logger.info(
            "Structured signal log written",
            extra={"symbol": signal.symbol, "signal_id": feature_row.signal_id, "path": str(self.signal_store.path)},
        )
        return payload

    def log_order(
        self,
        *,
        action: str,
        signal: TradeSignal,
        proposal: OrderRequest,
        risk: RiskDecision | dict[str, Any] | None,
        order: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        risk_payload = risk.to_dict() if hasattr(risk, "to_dict") else dict(risk or {})
        event = StructuredEvent(
            event_type="order",
            payload={
                "signal_id": str((signal.metrics or {}).get("signal_id") or ""),
                "cycle_id": str((signal.metrics or {}).get("cycle_id") or ""),
                "symbol": signal.symbol,
                "asset_class": signal.asset_class.value,
                "strategy_name": signal.strategy_name,
                "action": action,
                "classification": normalize_outcome_classification(action, risk_payload.get("rule")),
                "proposal": {
                    "symbol": proposal.symbol,
                    "side": proposal.side.value if hasattr(proposal.side, "value") else str(proposal.side),
                    "quantity": proposal.quantity,
                    "notional": proposal.notional,
                    "price": proposal.price,
                    "time_in_force": proposal.time_in_force,
                    "order_type": proposal.order_type,
                    "is_dry_run": proposal.is_dry_run,
                },
                "risk": risk_payload,
                "order": dict(order or {}),
            },
        )
        payload = event.to_dict()
        self.order_store.append(payload)
        logger.info(
            "Structured order log written",
            extra={"symbol": signal.symbol, "action": action, "path": str(self.order_store.path)},
        )
        return payload


_trade_logger: TradeLogger | None = None


def get_trade_logger(settings: Settings | None = None) -> TradeLogger:
    global _trade_logger
    resolved_settings = settings or get_settings()
    if _trade_logger is None or _trade_logger.settings is not resolved_settings:
        _trade_logger = TradeLogger(resolved_settings)
    return _trade_logger


def reset_trade_logger() -> None:
    global _trade_logger
    _trade_logger = None
