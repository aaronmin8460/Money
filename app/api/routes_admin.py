from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.api.schemas import ResetLocalStateRequest
from app.domain.models import AssetClass
from app.monitoring.discord_notifier import get_discord_notifier
from app.risk.risk_manager import RiskDecision
from app.services.broker import OrderRequest
from app.services.local_state_reset import LocalStateResetOptions, reset_local_state
from app.services.runtime import get_runtime
from app.strategies.base import Signal, TradeSignal

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
        "broker_backend": settings.broker_backend,
        "trading_enabled": settings.trading_enabled,
        "live_trading_enabled": settings.live_trading_enabled,
        "short_selling_enabled": settings.short_selling_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "active_strategy": settings.active_strategy,
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
        "max_position_notional": settings.max_position_notional,
        "position_notional_buffer_pct": settings.position_notional_buffer_pct,
        "effective_max_position_notional": settings.effective_max_position_notional,
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
    runtime = get_runtime()
    registry = runtime.strategy_registry
    strategies = []
    for strategy in registry.list_available():
        strategies.append(
            {
                "name": strategy.name,
                "supported_asset_classes": sorted(item.value for item in strategy.supported_asset_classes),
                "signal_only": strategy.signal_only,
                "active": strategy.name == runtime.settings.active_strategy,
            }
        )
    return {"strategies": strategies}


@router.get("/diagnostics/strategy")
def diagnostics_strategy() -> dict[str, Any]:
    runtime = get_runtime()
    strategy = runtime.strategy
    trader_status = runtime.get_auto_trader().get_status()
    return {
        "active_strategy": runtime.settings.active_strategy,
        "broker_mode": runtime.settings.broker_mode,
        "broker_backend": runtime.settings.broker_backend,
        "supported_asset_classes": sorted(item.value for item in strategy.supported_asset_classes),
        "signal_only": strategy.signal_only,
        "latest_signals": trader_status["last_signals"],
        "latest_scanned_symbols": trader_status["last_scanned_symbols"],
        "available_strategies": diagnostics_strategies()["strategies"],
    }


@router.get("/diagnostics/auto")
def diagnostics_auto() -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    trader = runtime.get_auto_trader()
    status = trader.get_status()
    account = runtime.broker.get_account()
    return {
        "trading_enabled": runtime.settings.trading_enabled,
        "auto_trade_enabled": runtime.settings.auto_trade_enabled,
        "enabled": status["enabled"],
        "running": status["running"],
        "broker_mode": runtime.settings.broker_mode,
        "broker_backend": runtime.settings.broker_backend,
        "active_strategy": runtime.settings.active_strategy,
        "market_open": status["market_open"],
        "allow_extended_hours": runtime.settings.allow_extended_hours,
        "scan_interval_seconds": runtime.settings.scan_interval_seconds,
        "last_run_time": status["last_run_time"],
        "last_run_result": status["last_run_result"],
        "cooldown_status": runtime.risk_manager.get_active_cooldowns(),
        "open_positions": runtime.portfolio.positions_diagnostics(),
        "account": {
            "cash": account.cash,
            "equity": account.equity,
            "buying_power": account.buying_power,
            "positions": account.positions,
            "mode": account.mode,
            "trading_enabled": account.trading_enabled,
        },
        "latest_evaluated_symbols": status["last_scanned_symbols"],
        "latest_signals": status["last_signals"],
        "latest_accepted_order_candidate": status["last_accepted_candidate"],
        "latest_rejected_order_candidate": status["last_rejected_candidate"],
        "latest_rejection": status["last_rejection"],
        "last_rejection_reason": status["last_rejection_reason"],
    }


@router.get("/diagnostics/risk")
def diagnostics_risk(limit: int = 10) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return runtime.risk_manager.get_diagnostics(limit=limit)


@router.get("/diagnostics/portfolio")
def diagnostics_portfolio() -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    broker_positions = []
    for position in runtime.broker.get_positions():
        position_side = str(position.get("side", "")).upper()
        quantity = float(position.get("qty", position.get("quantity", 0.0)) or 0.0)
        is_long = quantity > 0 and position_side not in {"SELL", "SHORT"}
        broker_positions.append(
            {
                **position,
                "is_long": is_long,
                "sellable": is_long and quantity > 0,
            }
        )
    broker_account = runtime.broker.get_account()
    return {
        "broker_account": {
            "cash": broker_account.cash,
            "equity": broker_account.equity,
            "buying_power": broker_account.buying_power,
            "positions": broker_account.positions,
            "mode": broker_account.mode,
            "trading_enabled": broker_account.trading_enabled,
        },
        "daily_baseline_equity": runtime.portfolio.daily_baseline_equity,
        "daily_baseline_date": (
            runtime.portfolio.daily_baseline_date.isoformat()
            if runtime.portfolio.daily_baseline_date is not None
            else None
        ),
        "tracked_local_positions": runtime.portfolio.positions_diagnostics(),
        "broker_positions": broker_positions,
    }


@router.get("/diagnostics/rejections/latest")
def diagnostics_rejections_latest(limit: int = 10) -> dict[str, Any]:
    runtime = get_runtime()
    return runtime.risk_manager.get_rejection_snapshot(limit=limit)


@router.post("/admin/reset-local-state")
def admin_reset_local_state(
    request: ResetLocalStateRequest | None = Body(default=None),
) -> dict[str, Any]:
    runtime = get_runtime()
    payload = request or ResetLocalStateRequest()
    try:
        return reset_local_state(
            LocalStateResetOptions(
                close_positions=payload.close_positions,
                cancel_open_orders=payload.cancel_open_orders,
                wipe_local_db=payload.wipe_local_db,
                reset_daily_baseline_to_current_equity=payload.reset_daily_baseline_to_current_equity,
            ),
            runtime=runtime,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/admin/notifications/test")
def admin_notifications_test() -> dict[str, Any]:
    runtime = get_runtime()
    settings = runtime.settings
    if settings.app_env.lower() != "development":
        raise HTTPException(status_code=403, detail="Debug notification endpoint is only available in development.")

    notifier = get_discord_notifier(settings)
    trade_action = "dry_run" if not settings.trading_enabled else "submitted"
    signal = TradeSignal(
        symbol="BTC/USD",
        signal=Signal.BUY,
        asset_class=AssetClass.CRYPTO,
        strategy_name="equity_momentum_breakout",
        price=65000.0,
        reason="debug notification test",
    )
    proposal = OrderRequest(
        symbol="BTC/USD",
        side=Signal.BUY.value,
        quantity=0.001,
        asset_class=AssetClass.CRYPTO,
        price=65000.0,
        time_in_force="gtc",
        is_dry_run=trade_action == "dry_run",
    )
    order = {
        "id": "debug-notification-order",
        "status": "DRY_RUN" if trade_action == "dry_run" else "FILLED",
        "symbol": "BTC/USD",
        "asset_class": AssetClass.CRYPTO.value,
        "side": Signal.BUY.value,
        "quantity": 0.001,
        "price": 65000.0,
        "is_dry_run": trade_action == "dry_run",
        "executed_at": "2026-04-10T14:05:00Z",
    }
    risk = RiskDecision(True, "Sample trade approved.", rule="approved")

    results = {
        "application_start": notifier.send_system_notification(
            event="Bot started",
            reason="debug endpoint start notification",
            category="start_stop",
        ),
        "application_stop": notifier.send_system_notification(
            event="Bot stopped",
            reason="debug endpoint stop notification",
            category="start_stop",
        ),
        "trade": notifier.send_trade_notification(
            action=trade_action,
            signal=signal,
            proposal=proposal,
            risk=risk,
            order=order,
        ),
    }
    return {
        "debug_only": True,
        "notifications_enabled": notifier.enabled,
        "trade_action": trade_action,
        "results": results,
    }
