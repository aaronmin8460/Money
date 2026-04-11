from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from app.config.settings import get_settings


_STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


def _record_extra_payload(record: logging.LogRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_LOG_RECORD_FIELDS or key in {"message", "asctime"}:
            continue
        if key.startswith("_"):
            continue
        payload[key] = value
    return payload


class StructuredConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = _record_extra_payload(record)
        if not extra:
            return base
        return f"{base} | {json.dumps(extra, default=str, sort_keys=True)}"


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "extra": _record_extra_payload(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def init_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        StructuredConsoleFormatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log_path = Path(settings.log_dir) / "app.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        JsonLineFormatter(
            "%(asctime)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(
        StructuredConsoleFormatter(
            formatter._fmt,  # type: ignore[attr-defined]
            datefmt=formatter.datefmt,
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        root.handlers = []
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.debug("Logging initialized", extra={"log_level": settings.log_level, "app_log_path": str(log_path)})


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
