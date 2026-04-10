from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.runtime import get_runtime

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/bars")
def market_bars(
    symbol: str,
    asset_class: str,
    timeframe: str = "1D",
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, object]:
    bars = get_runtime().market_data_service.get_bars(symbol, asset_class, timeframe, limit)
    return {"bars": [bar.to_dict() for bar in bars]}


@router.get("/quote")
def market_quote(symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_latest_quote(symbol, asset_class).to_dict()


@router.get("/trade")
def market_trade(symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_latest_trade(symbol, asset_class).to_dict()


@router.get("/snapshot")
def market_snapshot(symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_snapshot(symbol, asset_class)


@router.get("/session")
def market_session(asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_session_status(asset_class).to_dict()
