from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.runtime import get_runtime

router = APIRouter(prefix="/scanner", tags=["scanner"])


def _scan(asset_class: str | None = None, limit: int = 10) -> dict[str, object]:
    return get_runtime().scanner.scan(asset_class=asset_class, limit=limit).to_dict()


@router.get("/overview")
def scanner_overview(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> dict[str, object]:
    return get_runtime().market_overview.get_overview(asset_class=asset_class, limit=limit)


@router.get("/top-gainers")
def scanner_top_gainers(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["top_gainers"]


@router.get("/top-losers")
def scanner_top_losers(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["top_losers"]


@router.get("/breakouts")
def scanner_breakouts(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["breakouts"]


@router.get("/momentum")
def scanner_momentum(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["momentum"]


@router.get("/volatility")
def scanner_volatility(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["volatility"]


@router.get("/opportunities")
def scanner_opportunities(asset_class: str | None = None, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, object]]:
    return _scan(asset_class, limit)["opportunities"]


@router.get("/asset-class/{asset_class}")
def scanner_by_asset_class(asset_class: str, limit: int = Query(10, ge=1, le=100)) -> dict[str, object]:
    return _scan(asset_class, limit)
