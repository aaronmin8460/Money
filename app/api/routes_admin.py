from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.runtime import get_runtime

router = APIRouter(tags=["admin"])


@router.get("/health")
def health() -> dict[str, str]:
    runtime = get_runtime()
    return {"status": "ok", "mode": runtime.settings.broker_mode}


@router.get("/config")
def config() -> dict[str, Any]:
    settings = get_runtime().settings
    return {
        "app_env": settings.app_env,
        "broker_mode": settings.broker_mode,
        "trading_enabled": settings.trading_enabled,
        "live_trading_enabled": settings.live_trading_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "default_symbols": settings.default_symbols,
        "enabled_asset_classes": sorted(item.value for item in settings.enabled_asset_class_set),
        "universe_scan_enabled": settings.universe_scan_enabled,
        "universe_refresh_minutes": settings.universe_refresh_minutes,
        "scan_interval_seconds": settings.scan_interval_seconds,
        "scan_interval_seconds_by_asset_class": settings.scan_interval_seconds_by_asset_class,
        "max_risk_per_trade": settings.max_risk_per_trade,
        "max_total_exposure": settings.max_total_exposure,
        "max_positions_total": settings.max_positions_total,
        "max_positions_per_asset_class": settings.max_positions_per_asset_class,
        "max_notional_per_position": settings.max_notional_per_position,
        "max_notional_per_asset_class": settings.max_notional_per_asset_class,
        "max_daily_loss": settings.max_daily_loss,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "min_dollar_volume": settings.min_dollar_volume,
        "min_price": settings.min_price,
        "min_avg_volume": settings.min_avg_volume,
        "max_spread_pct": settings.max_spread_pct,
        "watchlists": settings.watchlists,
        "excluded_symbols": settings.excluded_symbols,
        "included_symbols": settings.included_symbols,
    }


@router.get("/diagnostics/universe")
def diagnostics_universe() -> dict[str, Any]:
    runtime = get_runtime()
    assets = runtime.asset_catalog.get_scan_universe()
    return {
        "stats": runtime.asset_catalog.get_stats(),
        "sample_assets": [asset.to_dict() for asset in assets[:20]],
    }


@router.get("/diagnostics/data-feed")
def diagnostics_data_feed(symbol: str | None = None, asset_class: str | None = None) -> dict[str, Any]:
    runtime = get_runtime()
    resolved_symbol = symbol or (runtime.settings.manual_symbols[0] if runtime.settings.manual_symbols else "AAPL")
    resolved_asset_class = asset_class or "equity"
    return {
        "symbol": resolved_symbol,
        "asset_class": resolved_asset_class,
        "quote": runtime.market_data_service.get_latest_quote(resolved_symbol, resolved_asset_class).to_dict(),
        "trade": runtime.market_data_service.get_latest_trade(resolved_symbol, resolved_asset_class).to_dict(),
        "session": runtime.market_data_service.get_session_status(resolved_asset_class).to_dict(),
    }


@router.get("/diagnostics/strategies")
def diagnostics_strategies() -> dict[str, Any]:
    registry = get_runtime().strategy_registry
    strategies = []
    for strategy in registry._strategies:
        strategies.append(
            {
                "name": strategy.name,
                "supported_asset_classes": sorted(item.value for item in strategy.supported_asset_classes),
                "signal_only": strategy.signal_only,
            }
        )
    return {"strategies": strategies}
