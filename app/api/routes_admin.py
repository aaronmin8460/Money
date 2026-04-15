from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.api.admin_auth import require_admin_auth
from app.api.rate_limit import rate_limit_admin, rate_limit_health
from app.api.schemas import ResetLocalStateRequest, RuntimeSafetyActionRequest, RuntimeSafetyResumeRequest
from app.config.settings import get_settings
from app.db.session import check_database_connection
from app.domain.models import AssetClass
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import get_logger
from app.risk.risk_manager import RiskDecision
from app.services.broker import OrderRequest
from app.services.local_state_reset import LocalStateResetOptions, reset_local_state
from app.services.runtime import get_runtime, probe_runtime
from app.strategies.base import Signal, TradeSignal

router = APIRouter(tags=["admin"])
protected_router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin_auth)])
logger = get_logger("api.admin")


@router.get("/health")
@rate_limit_health()
def health(request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.broker_mode,
        "trading_profile": settings.effective_trading_profile,
        "enabled_news_sources": settings.enabled_news_sources,
        "news_llm_status": settings.news_llm_status,
        "rate_limit_enabled": settings.rate_limit_enabled,
    }


@router.get("/health/ready")
@rate_limit_health()
def health_ready(request: Request, response: Response) -> JSONResponse:
    try:
        settings = get_settings()
        settings.validate_settings()
        probe_runtime(settings)
        if not check_database_connection(settings):
            raise RuntimeError("database connectivity check failed")
    except Exception as exc:
        logger.warning(
            "Readiness check failed",
            extra={"error_type": type(exc).__name__},
        )
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "trading_profile": settings.effective_trading_profile,
            "enabled_news_sources": settings.enabled_news_sources,
            "news_llm_status": settings.news_llm_status,
            "rate_limit_enabled": settings.rate_limit_enabled,
        },
    )


@protected_router.get("/config")
@rate_limit_admin()
def config(request: Request, response: Response) -> dict[str, Any]:
    settings = get_runtime().settings
    return {
        "app_env": settings.app_env,
        "log_dir": settings.log_dir,
        "broker_mode": settings.broker_mode,
        "broker_backend": settings.broker_backend,
        "trading_profile": settings.effective_trading_profile,
        "trading_profile_summary": settings.trading_profile_summary,
        "trading_enabled": settings.trading_enabled,
        "order_submission_mode": settings.order_submission_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "short_selling_enabled": settings.effective_short_selling_enabled,
        "auto_trade_enabled": settings.auto_trade_enabled,
        "active_strategy": settings.active_strategy,
        "primary_runtime_strategy": settings.primary_runtime_strategy,
        "active_strategy_by_asset_class": settings.active_strategy_by_asset_class,
        "candidate_strategies_by_asset_class": settings.resolved_trading_profile.candidate_strategies_by_asset_class,
        "default_symbols": settings.default_symbols,
        "active_symbols": settings.active_symbols,
        "active_crypto_symbols": settings.active_crypto_symbols,
        "enabled_asset_classes": settings.active_asset_classes,
        "crypto_only_mode": settings.crypto_only_mode,
        "crypto_symbols": settings.crypto_symbols,
        "primary_runtime_asset_class": settings.primary_runtime_asset_class.value,
        "universe_scan_enabled": settings.universe_scan_enabled,
        "universe_refresh_minutes": settings.universe_refresh_minutes,
        "scan_interval_seconds": settings.effective_scan_interval_seconds,
        "scan_interval_seconds_by_asset_class": settings.resolved_trading_profile.scan_interval_seconds_by_asset_class,
        "max_risk_per_trade": settings.max_risk_per_trade,
        "effective_risk_per_trade_pct": settings.effective_risk_per_trade_pct,
        "max_total_exposure": settings.max_total_exposure,
        "max_positions_total": settings.effective_max_positions_total,
        "max_positions_per_asset_class": settings.resolved_trading_profile.max_positions_per_asset_class,
        "max_position_notional": settings.max_position_notional,
        "position_notional_buffer_pct": settings.position_notional_buffer_pct,
        "effective_max_position_notional": settings.effective_max_position_notional,
        "entry_tranches": settings.entry_tranches,
        "entry_tranche_weights": settings.entry_tranche_weights,
        "scale_in_mode": settings.effective_scale_in_mode,
        "min_bars_between_tranches": settings.effective_min_bars_between_tranches,
        "minutes_between_tranches": settings.effective_minutes_between_tranches,
        "add_on_favorable_move_pct": settings.effective_add_on_favorable_move_pct,
        "allow_average_down": settings.allow_average_down,
        "max_notional_per_position": settings.max_notional_per_position,
        "max_notional_per_asset_class": settings.max_notional_per_asset_class,
        "max_daily_loss": settings.max_daily_loss,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "min_dollar_volume": settings.min_dollar_volume,
        "min_price": settings.min_price,
        "min_avg_volume": settings.min_avg_volume,
        "max_spread_pct": settings.max_spread_pct,
        "quote_stale_after_seconds": settings.quote_stale_after_seconds,
        "allow_extended_hours": settings.effective_allow_extended_hours,
        "watchlists": settings.watchlists,
        "excluded_symbols": settings.excluded_symbols,
        "included_symbols": settings.included_symbols,
        "discord_notify_holds_manual": settings.discord_notify_holds_manual,
        "discord_notify_scan_summary": settings.discord_notify_scan_summary,
        "discord_notify_crypto": settings.discord_notify_crypto,
        "discord_timezone": settings.discord_timezone,
        "halt_on_consecutive_losses": settings.halt_on_consecutive_losses,
        "max_consecutive_losing_exits": settings.max_consecutive_losing_exits,
        "halt_on_reconcile_mismatch": settings.halt_on_reconcile_mismatch,
        "halt_on_startup_sync_failure": settings.halt_on_startup_sync_failure,
        "ml_enabled": settings.ml_enabled,
        "ml_model_type": settings.ml_model_type,
        "ml_min_score_threshold": settings.effective_ml_min_score_threshold,
        "ml_min_train_rows": settings.ml_min_train_rows,
        "ml_retrain_enabled": settings.ml_retrain_enabled,
        "ml_promotion_min_auc": settings.ml_promotion_min_auc,
        "ml_promotion_min_precision": settings.ml_promotion_min_precision,
        "ml_promotion_min_winrate_lift": settings.ml_promotion_min_winrate_lift,
        "model_dir": settings.model_dir,
        "ml_current_model_path": settings.ml_current_model_path,
        "ml_candidate_model_path": settings.ml_candidate_model_path,
        "ml_registry_path": settings.ml_registry_path,
        "news_features_enabled": settings.news_features_enabled,
        "news_rss_enabled": settings.news_rss_enabled,
        "news_llm_enabled": settings.news_llm_enabled,
        "news_llm_available": settings.news_llm_available,
        "news_llm_status": settings.news_llm_status,
        "enabled_news_sources": settings.enabled_news_sources,
        "news_source_ids": settings.news_source_ids,
        "benzinga_rss_enabled": settings.benzinga_rss_enabled,
        "benzinga_rss_urls": settings.benzinga_rss_urls,
        "sec_rss_enabled": settings.sec_rss_enabled,
        "sec_rss_urls": settings.sec_rss_urls,
        "sec_user_agent": settings.sec_user_agent,
        "news_fetch_timeout_seconds": settings.news_fetch_timeout_seconds,
        "news_fetch_retry_count": settings.news_fetch_retry_count,
        "news_fetch_backoff_seconds": settings.news_fetch_backoff_seconds,
        "news_dedupe_window_minutes": settings.news_dedupe_window_minutes,
        "news_source_weights": settings.news_source_weights,
        "news_enable_source_diversity_features": settings.news_enable_source_diversity_features,
        "openai_model": settings.openai_model,
        "news_max_headlines_per_ticker": settings.news_max_headlines_per_ticker,
        "news_lookback_hours": settings.news_lookback_hours,
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_default": settings.rate_limit_default,
        "rate_limit_storage_uri": settings.rate_limit_storage_uri,
        "rate_limit_headers_enabled": settings.rate_limit_headers_enabled,
        "rate_limit_scanner": settings.rate_limit_scanner,
        "rate_limit_admin": settings.rate_limit_admin,
        "rate_limit_market": settings.rate_limit_market,
        "rate_limit_signals": settings.rate_limit_signals,
        "rate_limit_health_exempt": settings.rate_limit_health_exempt,
        "auto_trader_lock_path": settings.auto_trader_lock_path,
    }


@protected_router.get("/diagnostics/universe")
@rate_limit_admin()
def diagnostics_universe(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    assets = runtime.asset_catalog.get_scan_universe()
    return {
        "crypto_only_mode": runtime.settings.crypto_only_mode,
        "active_symbols": runtime.settings.active_symbols,
        "active_asset_classes": runtime.settings.active_asset_classes,
        "stats": runtime.asset_catalog.get_stats(),
        "sample_assets": [asset.to_dict() for asset in assets[:20]],
    }


@protected_router.get("/diagnostics/data-feed")
@rate_limit_admin()
def diagnostics_data_feed(request: Request, response: Response, symbol: str | None = None, asset_class: str | None = None) -> dict[str, Any]:
    runtime = get_runtime()
    resolved_symbol = symbol.strip().upper() if symbol else None
    if not resolved_symbol:
        active_symbols = runtime.settings.active_symbols
        if not active_symbols:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No symbol provided and no configured ACTIVE/DEFAULT symbols are available. "
                    "Pass ?symbol=... explicitly."
                ),
            )
        resolved_symbol = active_symbols[0]
    resolved_asset_class = asset_class or runtime.settings.primary_runtime_asset_class.value
    return {
        "symbol": resolved_symbol,
        "asset_class": resolved_asset_class,
        "quote": runtime.market_data_service.get_latest_quote(resolved_symbol, resolved_asset_class).to_dict(),
        "trade": runtime.market_data_service.get_latest_trade(resolved_symbol, resolved_asset_class).to_dict(),
        "session": runtime.market_data_service.get_session_status(resolved_asset_class).to_dict(),
        "normalized_snapshot": runtime.market_data_service.get_normalized_snapshot(
            resolved_symbol,
            resolved_asset_class,
        ).to_dict(),
    }


@protected_router.get("/diagnostics/strategies")
@rate_limit_admin()
def diagnostics_strategies(request: Request, response: Response) -> dict[str, Any]:
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


@protected_router.get("/diagnostics/strategy")
@rate_limit_admin()
def diagnostics_strategy(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    strategy_name = runtime.settings.primary_runtime_strategy
    strategy = runtime.strategy_registry.get(strategy_name)
    trader_status = runtime.get_auto_trader().get_status()
    available_strategies = []
    for candidate in runtime.strategy_registry.list_available():
        available_strategies.append(
            {
                "name": candidate.name,
                "supported_asset_classes": sorted(item.value for item in candidate.supported_asset_classes),
                "signal_only": candidate.signal_only,
                "active": candidate.name == runtime.settings.active_strategy,
            }
        )
    return {
        "active_strategy": runtime.settings.active_strategy,
        "trading_profile": runtime.settings.effective_trading_profile,
        "primary_runtime_strategy": strategy_name,
        "primary_runtime_asset_class": runtime.settings.primary_runtime_asset_class.value,
        "broker_mode": runtime.settings.broker_mode,
        "broker_backend": runtime.settings.broker_backend,
        "supported_asset_classes": sorted(item.value for item in strategy.supported_asset_classes),
        "signal_only": strategy.signal_only,
        "latest_signals": trader_status["last_signals"],
        "symbol_evaluations": trader_status["last_symbol_evaluations"],
        "latest_scanned_symbols": trader_status["last_scanned_symbols"],
        "strategy_routing": trader_status["strategy_routing"],
        "candidate_strategy_routing": trader_status["candidate_strategy_routing"],
        "available_strategies": available_strategies,
    }


@protected_router.get("/diagnostics/auto")
@rate_limit_admin()
def diagnostics_auto(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker(source="diagnostics_auto")
    trader = runtime.get_auto_trader()
    status = trader.get_status()
    account = runtime.broker.get_account()
    return {
        "trading_enabled": runtime.settings.trading_enabled,
        "auto_trade_enabled": runtime.settings.auto_trade_enabled,
        "trading_profile": runtime.settings.effective_trading_profile,
        "trading_profile_summary": runtime.settings.trading_profile_summary,
        "enabled": status["enabled"],
        "running": status["running"],
        "broker_mode": runtime.settings.broker_mode,
        "broker_backend": runtime.settings.broker_backend,
        "active_strategy": runtime.settings.active_strategy,
        "primary_runtime_strategy": status["primary_runtime_strategy"],
        "primary_runtime_asset_class": status["primary_runtime_asset_class"],
        "active_symbols": status["active_symbols"],
        "active_crypto_symbols": status["active_crypto_symbols"],
        "active_asset_classes": status["active_asset_classes"],
        "crypto_only_mode": status["crypto_only_mode"],
        "market_open": status["market_open"],
        "market_session_state": status["market_session_state"],
        "allow_extended_hours": runtime.settings.effective_allow_extended_hours,
        "scan_interval_seconds": runtime.settings.effective_scan_interval_seconds,
        "scan_summary_notifications_enabled": status["scan_summary_notifications_enabled"],
        "last_cycle_id": status["last_cycle_id"],
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
        "latest_rejected_reason": status["latest_rejected_reason"],
        "latest_skipped_reason": status["latest_skipped_reason"],
        "last_submitted_order": status["last_submitted_order"],
        "latest_rejection": status["last_rejection"],
        "last_rejection_reason": status["last_rejection_reason"],
        "latest_broker_order_status_updates": status["latest_broker_order_status_updates"],
        "broker_status_dedupe_suppressed": status["broker_status_dedupe_suppressed"],
        "summary_dedupe_suppressed": status["summary_dedupe_suppressed"],
        "recent_notification_ids": status["recent_notification_ids"],
        "notifier_diagnostics": status["notifier_diagnostics"],
        "thread_ident": status["thread_ident"],
        "tranche_state": status["tranche_state"],
        "symbol_evaluations": status["last_symbol_evaluations"],
        "strategy_routing": status["strategy_routing"],
        "scan_selection_mode": status["scan_selection_mode"],
        "scan_requested_symbols": status["scan_requested_symbols"],
        "scan_ranking_limit": status["scan_ranking_limit"],
        "latest_scan_scanned_count": status["last_scan_scanned_count"],
        "latest_ranked_candidate_count": status["last_ranked_candidate_count"],
        "latest_symbol_evaluation_count": status["last_symbol_evaluation_count"],
        "quote_stale_after_seconds": status["quote_stale_after_seconds"],
        "crypto_monitoring_active": status["crypto_monitoring_active"],
        "ml_enabled": status["ml_enabled"],
        "ml_model_type": status["ml_model_type"],
        "ml_min_score_threshold": status["ml_min_score_threshold"],
        "news_features_enabled": status["news_features_enabled"],
        "enabled_news_sources": status["enabled_news_sources"],
        "auto_trader_lock_path": status["auto_trader_lock_path"],
        "process_lock_acquired": status["process_lock_acquired"],
        "process_lock_metadata": status["process_lock_metadata"],
        "cycle_in_progress": status["cycle_in_progress"],
        "rate_limit_enabled": runtime.settings.rate_limit_enabled,
        "runtime_safety": status["runtime_safety"],
        "runtime_halted": status["runtime_halted"],
        "new_entries_allowed": status["new_entries_allowed"],
        "last_reconcile_status": status["last_reconcile_status"],
    }


@protected_router.get("/diagnostics/runtime-safety")
@rate_limit_admin()
def diagnostics_runtime_safety(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    trader = runtime.get_auto_trader()
    status = trader.get_status()
    return runtime.runtime_safety.get_runtime_diagnostics(
        lock_metadata=status["process_lock_metadata"],
        loop_metadata={
            "running": status["running"],
            "thread_ident": status["thread_ident"],
            "cycle_in_progress": status["cycle_in_progress"],
            "last_cycle_id": status["last_cycle_id"],
            "last_run_time": status["last_run_time"],
        },
    )


@protected_router.get("/diagnostics/reconciliation")
@rate_limit_admin()
def diagnostics_reconciliation(request: Request, response: Response, refresh: bool = True) -> dict[str, Any]:
    runtime = get_runtime()
    if refresh:
        runtime.sync_with_broker(source="diagnostics_reconciliation")
    snapshot = runtime.runtime_safety.get_reconciliation_snapshot()
    snapshot["tracked_local_positions"] = runtime.portfolio.positions_diagnostics()
    snapshot["broker_positions"] = runtime.broker.get_positions()
    snapshot["tranche_state"] = runtime.tranche_state.snapshot()
    return snapshot


@protected_router.get("/diagnostics/risk")
@rate_limit_admin()
def diagnostics_risk(request: Request, response: Response, limit: int = 10) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker(source="diagnostics_risk")
    return runtime.risk_manager.get_diagnostics(limit=limit)


@protected_router.get("/diagnostics/portfolio")
@rate_limit_admin()
def diagnostics_portfolio(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker(source="diagnostics_portfolio")
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
        "tranche_state": runtime.tranche_state.snapshot(),
    }


@protected_router.get("/diagnostics/tranches")
@rate_limit_admin()
def diagnostics_tranches(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    return {
        "active_strategy": runtime.settings.active_strategy,
        "tranche_state": runtime.tranche_state.snapshot(),
    }


@protected_router.get("/diagnostics/rejections/latest")
@rate_limit_admin()
def diagnostics_rejections_latest(request: Request, response: Response, limit: int = 10) -> dict[str, Any]:
    runtime = get_runtime()
    return runtime.risk_manager.get_rejection_snapshot(limit=limit)


@protected_router.post("/admin/reset-local-state")
@rate_limit_admin()
def admin_reset_local_state(
    request: Request,
    response: Response,
    payload: ResetLocalStateRequest | None = Body(default=None),
) -> dict[str, Any]:
    runtime = get_runtime()
    payload = payload or ResetLocalStateRequest()
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


@protected_router.post("/admin/runtime-safety/halt")
@rate_limit_admin()
def admin_runtime_safety_halt(
    request: Request,
    response: Response,
    payload: RuntimeSafetyActionRequest | None = Body(default=None),
) -> dict[str, Any]:
    runtime = get_runtime()
    payload = payload or RuntimeSafetyActionRequest()
    return runtime.runtime_safety.manual_halt(operator_note=payload.note)


@protected_router.post("/admin/runtime-safety/resume")
@rate_limit_admin()
def admin_runtime_safety_resume(
    request: Request,
    response: Response,
    payload: RuntimeSafetyResumeRequest | None = Body(default=None),
) -> dict[str, Any]:
    runtime = get_runtime()
    payload = payload or RuntimeSafetyResumeRequest()
    return runtime.runtime_safety.resume(
        operator_note=payload.note,
        reset_consecutive_losing_exits=payload.reset_consecutive_losing_exits,
    )


@protected_router.post("/admin/notifications/test")
@rate_limit_admin()
def admin_notifications_test(request: Request, response: Response) -> dict[str, Any]:
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


router.include_router(protected_router)
