from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.api.rate_limit import limiter
from app.config import settings as settings_module
from app.config.settings import Settings
from app.services.runtime import close_runtime, get_runtime
from app.services.scanner import ScanResult


def _lock_path() -> str:
    return f"{tempfile.gettempdir()}/money-rate-limit-{uuid.uuid4().hex}.lock"


def _build_settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "broker_mode": "mock",
        "trading_enabled": False,
        "auto_trade_enabled": False,
        "default_symbols": ["AAPL"],
        "max_positions": 1,
        "scan_interval_seconds": 1,
        "auto_trader_lock_path": _lock_path(),
        "rate_limit_enabled": True,
        "rate_limit_default": "5/minute",
        "rate_limit_scanner": "2/minute",
        "rate_limit_headers_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _empty_scan_result() -> ScanResult:
    return ScanResult(
        generated_at=datetime.now(timezone.utc),
        asset_class="equity",
        scanned_count=0,
        opportunities=[],
        top_gainers=[],
        top_losers=[],
        unusual_volume=[],
        breakouts=[],
        pullbacks=[],
        volatility=[],
        momentum=[],
        regime_status={},
        errors=[],
        symbol_snapshots={},
    )


def _reset_limiter() -> None:
    limiter.reset()
    storage = getattr(limiter, "_storage", None)
    if storage is not None and hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture(autouse=True)
def _reset_runtime_and_rate_limits():
    close_runtime()
    _reset_limiter()
    yield
    close_runtime()
    _reset_limiter()


def test_scanner_endpoint_returns_429_when_limit_exceeded(monkeypatch) -> None:
    settings_module._settings = _build_settings(rate_limit_scanner="2/minute")

    with TestClient(app) as client:
        runtime = get_runtime()
        monkeypatch.setattr(runtime.scanner, "scan", lambda *args, **kwargs: _empty_scan_result())

        first = client.get("/scanner/opportunities?limit=1")
        second = client.get("/scanner/opportunities?limit=1")
        third = client.get("/scanner/opportunities?limit=1")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert "X-RateLimit-Remaining" in first.headers
    assert third.json()["error"] == "rate_limit_exceeded"
    assert third.json()["path"] == "/scanner/opportunities"


def test_health_endpoint_is_exempt_when_configured(monkeypatch) -> None:
    settings_module._settings = _build_settings(
        rate_limit_default="1/minute",
        rate_limit_health_exempt=True,
    )

    with TestClient(app) as client:
        runtime = get_runtime()
        monkeypatch.setattr(runtime.scanner, "scan", lambda *args, **kwargs: _empty_scan_result())

        first = client.get("/health")
        second = client.get("/health")
        third = client.get("/health")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert first.json()["rate_limit_enabled"] is True


def test_rate_limiting_can_be_disabled(monkeypatch) -> None:
    settings_module._settings = _build_settings(
        rate_limit_enabled=False,
        rate_limit_scanner="1/minute",
    )

    with TestClient(app) as client:
        runtime = get_runtime()
        monkeypatch.setattr(runtime.scanner, "scan", lambda *args, **kwargs: _empty_scan_result())

        first = client.get("/scanner/opportunities?limit=1")
        second = client.get("/scanner/opportunities?limit=1")

    assert first.status_code == 200
    assert second.status_code == 200
