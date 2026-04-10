from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.runtime import get_runtime

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("")
def list_assets(
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
def search_assets(q: str = Query(..., min_length=1), limit: int = Query(25, ge=1, le=250)) -> list[dict[str, object]]:
    assets = get_runtime().asset_catalog.search_assets(q, limit=limit)
    return [asset.to_dict() for asset in assets]


@router.get("/stats")
def asset_stats() -> dict[str, int]:
    return get_runtime().asset_catalog.get_stats()


@router.post("/refresh")
def refresh_assets() -> dict[str, object]:
    result = get_runtime().asset_catalog.refresh(force=True)
    return {
        "asset_count": result.asset_count,
        "refreshed_at": result.refreshed_at.isoformat(),
        "cache_hit": result.cache_hit,
        "source": result.source,
    }


@router.get("/{symbol:path}")
def asset_detail(symbol: str) -> dict[str, object]:
    asset = get_runtime().asset_catalog.get_asset(symbol)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset '{symbol}' not found in catalog.")
    return asset.to_dict()
