from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.api.rate_limit import rate_limit_scanner
from app.services.runtime import get_runtime

router = APIRouter(prefix="/scanner", tags=["scanner"])


def _scan(asset_class: str | None = None, limit: int = 10) -> dict[str, object]:
    return get_runtime().scanner.scan(asset_class=asset_class, limit=limit).to_dict()


@router.get("/overview")
@rate_limit_scanner()
def scanner_overview(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> dict[str, object]:
    return get_runtime().market_overview.get_overview(asset_class=asset_class, limit=limit)


@router.get("/top-gainers")
@rate_limit_scanner()
def scanner_top_gainers(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["top_gainers"]


@router.get("/top-losers")
@rate_limit_scanner()
def scanner_top_losers(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["top_losers"]


@router.get("/breakouts")
@rate_limit_scanner()
def scanner_breakouts(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["breakouts"]


@router.get("/momentum")
@rate_limit_scanner()
def scanner_momentum(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["momentum"]


@router.get("/volatility")
@rate_limit_scanner()
def scanner_volatility(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["volatility"]


@router.get("/opportunities")
@rate_limit_scanner()
def scanner_opportunities(request: Request, response: Response, asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["opportunities"]


@router.get("/asset-class/{asset_class}")
@rate_limit_scanner()
def scanner_by_asset_class(request: Request, response: Response, asset_class: str, limit: int = Query(10, ge=1, le=100)) -> dict[str, object]:
    return _scan(asset_class, limit)
