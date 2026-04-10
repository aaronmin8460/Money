from __future__ import annotations

from fastapi import FastAPI

from app.api.routes_admin import router as admin_router
from app.api.routes_assets import router as assets_router
from app.api.routes_market import router as market_router
from app.api.routes import router
from app.api.routes_scanner import router as scanner_router
from app.api.routes_signals import router as signals_router
from app.config.settings import get_settings
from app.db.init_db import init_db
from app.monitoring.logger import init_logging, get_logger
from app.services.runtime import close_runtime, get_runtime

app = FastAPI(title="Money Trading Bot API")
app.include_router(router)
app.include_router(admin_router)
app.include_router(assets_router)
app.include_router(market_router)
app.include_router(scanner_router)
app.include_router(signals_router)


@app.on_event("startup")
def on_startup() -> None:
    init_logging()
    settings = get_settings()
    settings.validate_settings()
    init_db()
    logger = get_logger("api.startup")
    runtime = get_runtime(settings)
    if settings.auto_trade_enabled:
        started = runtime.get_auto_trader().start()
        logger.info(
            "AUTO_TRADE_ENABLED processed on startup",
            extra={"started": started, "broker_mode": settings.broker_mode},
        )
    logger.info("API startup complete", extra={"broker_mode": settings.broker_mode})


@app.on_event("shutdown")
def on_shutdown() -> None:
    close_runtime()
