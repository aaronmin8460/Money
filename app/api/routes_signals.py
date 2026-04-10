from __future__ import annotations

import json

from fastapi import APIRouter, Query

from app.api.schemas import SignalRunRequest
from app.db.models import NormalizedSignalRecord
from app.db.session import SessionLocal
from app.services.runtime import get_runtime

router = APIRouter(tags=["signals"])


def _serialize_signal(row: NormalizedSignalRecord) -> dict[str, object]:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "asset_class": row.asset_class,
        "strategy_name": row.strategy_name,
        "signal_type": row.signal_type,
        "direction": row.direction,
        "signal": row.signal,
        "confidence_score": row.confidence_score,
        "entry_price": row.entry_price,
        "stop_price": row.stop_price,
        "target_price": row.target_price,
        "position_size": row.position_size,
        "atr": row.atr,
        "momentum_score": row.momentum_score,
        "liquidity_score": row.liquidity_score,
        "spread_score": row.spread_score,
        "regime_state": row.regime_state,
        "reason": row.reason,
        "generated_at": row.generated_at.isoformat(),
        "metrics": json.loads(row.metrics_json) if row.metrics_json else {},
    }


@router.get("/signals")
def list_signals(limit: int = Query(50, ge=1, le=250)) -> list[dict[str, object]]:
    with SessionLocal() as session:
        rows = (
            session.query(NormalizedSignalRecord)
            .order_by(NormalizedSignalRecord.generated_at.desc())
            .limit(limit)
            .all()
        )
    return [_serialize_signal(row) for row in rows]


@router.post("/signals/run")
def run_signals(request: SignalRunRequest) -> dict[str, object]:
    trader = get_runtime().get_auto_trader()
    if request.symbol:
        return trader.run_symbol_now(request.symbol, request.asset_class)
    return trader.run_now()


@router.get("/signals/top")
def top_signals(limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    with SessionLocal() as session:
        rows = (
            session.query(NormalizedSignalRecord)
            .order_by(NormalizedSignalRecord.confidence_score.desc(), NormalizedSignalRecord.generated_at.desc())
            .limit(limit)
            .all()
        )
    return [_serialize_signal(row) for row in rows]
