from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.api.rate_limit import rate_limit_default
from app.services.runtime import get_runtime

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("")
@rate_limit_default()
def list_assets(
    request: Request,
    response: Response,
    asset_class: str | None = None,
    query: str | None = None,
    exchange: str | None = None,
    tradable: bool | None = None,
    limit: int = Query(100, ge=1, le=5000),
) -> list[dict[str, object]]:
    assets = get_runtime().asset_catalog.list_assets(
        asset_class=asset_class,
        query=query,
        exchange=exchange,
        tradable=tradable,
        limit=limit,
    )
    return [asset.to_dict() for asset in assets]


@router.get("/search")
@rate_limit_default()
def search_assets(request: Request, response: Response, q: str = Query(..., min_length=1), limit: int = Query(25, ge=1, le=250)) -> list[dict[str, object]]:
    assets = get_runtime().asset_catalog.search_assets(q, limit=limit)
    return [asset.to_dict() for asset in assets]


@router.get("/stats")
@rate_limit_default()
def asset_stats(request: Request, response: Response) -> dict[str, int]:
    return get_runtime().asset_catalog.get_stats()


@router.post("/refresh")
@rate_limit_default()
def refresh_assets(request: Request, response: Response) -> dict[str, object]:
    result = get_runtime().asset_catalog.refresh(force=True)
    return {
        "asset_count": result.asset_count,
        "refreshed_at": result.refreshed_at.isoformat(),
        "cache_hit": result.cache_hit,
        "source": result.source,
    }


@router.get("/{symbol:path}")
@rate_limit_default()
def asset_detail(request: Request, response: Response, symbol: str) -> dict[str, object]:
    asset = get_runtime().asset_catalog.get_asset(symbol)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset '{symbol}' not found in catalog.")
    return asset.to_dict()
