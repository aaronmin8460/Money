from __future__ import annotations

import logging
import sys

from app.config.settings import get_settings


def init_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        root.handlers = []
    root.addHandler(handler)
    root.debug("Logging initialized", extra={"log_level": settings.log_level})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
