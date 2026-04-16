from __future__ import annotations

import tempfile
import uuid

from fastapi.testclient import TestClient

from app.api.app import app
from app.config import settings as settings_module
from app.config.settings import Settings
from app.db.init_db import init_db
from app.db.models import FillRecord
from app.db.session import SessionLocal
from app.services.performance import calculate_max_drawdown, calculate_performance_summary


def _build_settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "broker_mode": "mock",
        "trading_enabled": False,
        "auto_trade_enabled": False,
        "live_trading_enabled": False,
        "discord_notifications_enabled": False,
        "api_admin_token": "test-admin-token",
        "auto_trader_lock_path": f"{tempfile.gettempdir()}/money-performance-test-{uuid.uuid4().hex}.lock",
    }
    values.update(overrides)
    return Settings(**values)


def _admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


def test_diagnostics_performance_returns_expected_metric_keys() -> None:
    settings_module._settings = _build_settings()

    with TestClient(app) as client:
        response = client.get("/diagnostics/performance", headers=_admin_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "insufficient_data"
    assert "metrics" in payload
    assert "sharpe_ratio" in payload["metrics"]
    assert "sortino_ratio" in payload["metrics"]
    assert "max_drawdown" in payload["metrics"]
    assert {"amount", "pct", "peak", "trough"} <= set(payload["metrics"]["max_drawdown"])
    assert "daily_pnl" in payload
    assert "weekly_pnl" in payload
    assert "counts" in payload
    assert "assumptions" in payload


def test_performance_summary_handles_empty_and_fill_only_state_gracefully() -> None:
    init_db()
    with SessionLocal() as session:
        empty_summary = calculate_performance_summary(session)
        assert empty_summary["status"] == "partial"
        assert empty_summary["source"] == "insufficient_data"
        assert empty_summary["metrics"]["sharpe_ratio"] is None
        assert empty_summary["metrics"]["sortino_ratio"] is None
        assert empty_summary["metrics"]["max_drawdown"] == {
            "amount": None,
            "pct": None,
            "peak": None,
            "trough": None,
        }
        assert empty_summary["daily_pnl"] == []
        assert empty_summary["weekly_pnl"] == []

        session.add(
            FillRecord(
                order_id="fill-only",
                symbol="AAPL",
                asset_class="equity",
                side="BUY",
                quantity=1.0,
                price=100.0,
            )
        )
        session.commit()

        summary = calculate_performance_summary(session)

    assert summary["status"] == "partial"
    assert summary["source"] == "insufficient_data"
    assert summary["metrics"]["sharpe_ratio"] is None
    assert summary["metrics"]["sortino_ratio"] is None
    assert summary["metrics"]["max_drawdown"] == {
        "amount": None,
        "pct": None,
        "peak": None,
        "trough": None,
    }
    assert summary["daily_pnl"] == []
    assert summary["weekly_pnl"] == []
    assert summary["counts"]["fills"] == 1
    assert any("fills do not contain realized P&L" in warning for warning in summary["warnings"])


def test_calculate_max_drawdown_is_deterministic() -> None:
    drawdown = calculate_max_drawdown([100.0, 120.0, 90.0, 110.0])

    assert drawdown == {
        "amount": 30.0,
        "pct": 0.25,
        "peak": 120.0,
        "trough": 90.0,
    }
