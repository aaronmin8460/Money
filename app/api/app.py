from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.rate_limit import configure_rate_limiter
from app.api.routes_admin import router as admin_router
from app.api.routes_assets import router as assets_router
from app.api.routes_market import router as market_router
from app.api.routes import router
from app.api.routes_scanner import router as scanner_router
from app.api.routes_signals import router as signals_router
from app.config.settings import get_settings
from app.db.init_db import init_db
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import init_logging, get_logger
from app.services.runtime import close_runtime, get_runtime

app = FastAPI(title="Money Trading Bot API")
app.include_router(router)
app.include_router(admin_router)
app.include_router(assets_router)
app.include_router(market_router)
app.include_router(scanner_router)
app.include_router(signals_router)


@app.middleware("http")
async def normalize_rate_limit_response(request, call_next):
    response = await call_next(request)
    if response.status_code != 429:
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    payload: dict[str, object] = {}
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}

    limit_description = str(
        payload.get("limit")
        or payload.get("detail")
        or payload.get("error")
        or "rate limit exceeded"
    )
    normalized = JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": "Too many requests. Please retry later.",
            "limit": limit_description,
            "path": request.url.path,
        },
    )
    for header_name in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"):
        header_value = response.headers.get(header_name)
        if header_value is not None:
            normalized.headers[header_name] = header_value
    return normalized


@app.on_event("startup")
def on_startup() -> None:
    init_logging()
    settings = get_settings()
    logger = get_logger("api.startup")
    configure_rate_limiter(app, settings)
    logger.info(
        "Application startup initiated",
        extra={
            "broker_mode": settings.broker_mode,
            "broker_backend": settings.broker_backend,
            "order_submission_mode": settings.order_submission_mode,
            "trading_profile": settings.effective_trading_profile,
            "auto_trade_enabled": settings.auto_trade_enabled,
            "auto_trader_lock_path": settings.auto_trader_lock_path,
        },
    )
    settings.validate_settings()
    init_db()
    runtime = get_runtime(settings)
    logger.info(
        "Application startup configuration",
        extra={
            "broker_mode": settings.broker_mode,
            "broker_backend": settings.broker_backend,
            "active_strategy": settings.active_strategy,
            "trading_profile": settings.effective_trading_profile,
            "trading_profile_summary": settings.trading_profile_summary,
            "trading_enabled": settings.trading_enabled,
            "order_submission_mode": settings.order_submission_mode,
            "auto_trade_enabled": settings.auto_trade_enabled,
            "discord_enabled": settings.discord_notifications_enabled,
            "auto_trader_lock_path": settings.auto_trader_lock_path,
            "news_llm_status": settings.news_llm_status,
            "enabled_news_sources": settings.enabled_news_sources,
            "rate_limit_enabled": settings.rate_limit_enabled,
            "rate_limit_default": settings.rate_limit_default,
        },
    )
    notifier = get_discord_notifier(settings)
    if settings.auto_trade_enabled:
        logger.info(
            "AUTO_TRADE_ENABLED is true; attempting in-process auto-trader startup",
            extra={
                "order_submission_mode": settings.order_submission_mode,
                "auto_trader_lock_path": settings.auto_trader_lock_path,
            },
        )
        trader = runtime.get_auto_trader()
        started = trader.start()
        trader_status = trader.get_status()
        logger.info(
            "AUTO_TRADE_ENABLED processed on startup",
            extra={
                "started": started,
                "broker_mode": settings.broker_mode,
                "broker_backend": settings.broker_backend,
                "active_strategy": settings.active_strategy,
                "trading_profile": settings.effective_trading_profile,
                "order_submission_mode": settings.order_submission_mode,
                "auto_trader_running": trader_status["running"],
                "process_lock_acquired": trader_status["process_lock_acquired"],
                "process_lock_metadata": trader_status["process_lock_metadata"],
            },
        )
    else:
        logger.info(
            "AUTO_TRADE_ENABLED is false; API started without launching the background trader loop",
            extra={
                "order_submission_mode": settings.order_submission_mode,
                "auto_trader_lock_path": settings.auto_trader_lock_path,
            },
        )
        notifier.send_system_notification(
            event="Bot started",
            reason="application startup completed",
            details={
                "broker_mode": settings.broker_mode,
                "active_strategy": settings.active_strategy,
                "trading_profile": settings.effective_trading_profile,
                "trading_enabled": settings.trading_enabled,
                "order_submission_mode": settings.order_submission_mode,
                "auto_trade_enabled": settings.auto_trade_enabled,
                "discord_enabled": settings.discord_notifications_enabled,
            },
            category="start_stop",
        )
    logger.info(
        "API startup complete",
        extra={
            "broker_mode": settings.broker_mode,
            "broker_backend": settings.broker_backend,
            "active_strategy": settings.active_strategy,
            "trading_profile": settings.effective_trading_profile,
            "trading_enabled": settings.trading_enabled,
            "order_submission_mode": settings.order_submission_mode,
            "auto_trade_enabled": settings.auto_trade_enabled,
            "discord_enabled": settings.discord_notifications_enabled,
        },
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    settings = get_settings()
    logger = get_logger("api.shutdown")
    notifier = get_discord_notifier(settings)
    runtime = get_runtime(settings)
    trader_status = runtime.get_auto_trader().get_status()
    was_auto_trader_running = trader_status.get("running", False)
    logger.info(
        "Application shutdown initiated",
        extra={
            "broker_mode": settings.broker_mode,
            "broker_backend": settings.broker_backend,
            "active_strategy": settings.active_strategy,
            "trading_profile": settings.effective_trading_profile,
            "order_submission_mode": settings.order_submission_mode,
            "auto_trader_running": was_auto_trader_running,
            "process_lock_acquired": trader_status.get("process_lock_acquired"),
            "process_lock_metadata": trader_status.get("process_lock_metadata"),
        },
    )
    try:
        close_runtime()
    finally:
        logger.info(
            "Application shutdown complete",
            extra={
                "broker_mode": settings.broker_mode,
                "broker_backend": settings.broker_backend,
                "active_strategy": settings.active_strategy,
                "trading_profile": settings.effective_trading_profile,
                "order_submission_mode": settings.order_submission_mode,
            },
        )
        if not was_auto_trader_running:
            notifier.send_system_notification(
                event="Bot stopped",
                reason="application shutdown completed",
                details={
                    "broker_mode": settings.broker_mode,
                    "active_strategy": settings.active_strategy,
                    "trading_profile": settings.effective_trading_profile,
                    "trading_enabled": settings.trading_enabled,
                    "order_submission_mode": settings.order_submission_mode,
                    "auto_trade_enabled": settings.auto_trade_enabled,
                    "discord_enabled": settings.discord_notifications_enabled,
                },
                category="start_stop",
            )
