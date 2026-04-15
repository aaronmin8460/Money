from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.api.rate_limit import rate_limit_market
from app.services.runtime import get_runtime

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/bars")
@rate_limit_market()
def market_bars(
    request: Request,
    response: Response,
    symbol: str,
    asset_class: str,
    timeframe: str = "1D",
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, object]:
    bars = get_runtime().market_data_service.get_bars(symbol, asset_class, timeframe, limit)
    return {"bars": [bar.to_dict() for bar in bars]}


@router.get("/quote")
@rate_limit_market()
def market_quote(request: Request, response: Response, symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_latest_quote(symbol, asset_class).to_dict()


@router.get("/trade")
@rate_limit_market()
def market_trade(request: Request, response: Response, symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_latest_trade(symbol, asset_class).to_dict()


@router.get("/snapshot")
@rate_limit_market()
def market_snapshot(request: Request, response: Response, symbol: str, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_snapshot(symbol, asset_class)


@router.get("/session")
@rate_limit_market()
def market_session(request: Request, response: Response, asset_class: str) -> dict[str, object]:
    return get_runtime().market_data_service.get_session_status(asset_class).to_dict()
