from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import router
from app.config.settings import get_settings
from app.db.init_db import init_db
from app.monitoring.logger import init_logging, get_logger

app = FastAPI(title="Money Trading Bot API")
app.include_router(router)


@app.on_event("startup")
def on_startup() -> None:
    init_logging()
    settings = get_settings()
    settings.validate_settings()
    init_db()
    logger = get_logger("api.startup")
    logger.info("API startup complete", extra={"broker_mode": settings.broker_mode})
