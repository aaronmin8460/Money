from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import app
from app.config import settings as settings_module
from app.config.settings import Settings
from app.services.runtime import get_runtime
from app.strategies.base import Signal, TradeSignal


def build_settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "broker_mode": "paper",
        "trading_enabled": False,
        "auto_trade_enabled": False,
        "default_symbols": ["AAPL", "SPY"],
        "max_positions": 3,
        "max_position_notional": 10_000.0,
        "max_risk_per_trade": 0.05,
        "scan_interval_seconds": 1,
    }
    values.update(overrides)
    return Settings(**values)


def test_broker_status_route() -> None:
    settings_module._settings = build_settings()
    with TestClient(app) as client:
        response = client.get("/broker/status")
    assert response.status_code == 200
    data = response.json()
    assert data["broker_mode"] == "paper"


def test_broker_account_route() -> None:
    settings_module._settings = build_settings()
    with TestClient(app) as client:
        response = client.get("/broker/account")
    assert response.status_code == 200
    data = response.json()
    assert data["cash"] == 100000.0
    assert data["equity"] == 100000.0


def test_run_once_persists_paper_state_across_routes(monkeypatch) -> None:
    settings_module._settings = build_settings(trading_enabled=True)
    with TestClient(app) as client:
        runtime = get_runtime()

        def fake_generate_signals(symbol: str, data: object) -> list[TradeSignal]:
            return [
                TradeSignal(
                    symbol=symbol,
                    signal=Signal.BUY,
                    price=100.0,
                    stop_price=95.0,
                    reason="test entry",
                )
            ]

        monkeypatch.setattr(runtime.strategy, "generate_signals", fake_generate_signals)

        run_once_response = client.post("/run-once", json={"symbol": "AAPL"})
        assert run_once_response.status_code == 200
        assert run_once_response.json()["action"] == "submitted"

        account_response = client.get("/broker/account")
        positions_response = client.get("/positions")
        orders_response = client.get("/orders")
        risk_response = client.get("/risk")
        strategy_positions_response = client.get("/strategy/positions")
        strategy_signals_response = client.get("/strategy/signals")
        auto_status_response = client.get("/auto/status")

    account = account_response.json()
    positions = positions_response.json()
    orders = orders_response.json()
    risk = risk_response.json()
    strategy_positions = strategy_positions_response.json()["positions"]
    signals = strategy_signals_response.json()["signals"]
    auto_status = auto_status_response.json()

    assert account["cash"] == 90000.0
    assert account["positions"] == 1
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert len(orders) == 1
    assert orders[0]["status"] == "FILLED"
    assert risk["cash"] == 90000.0
    assert risk["equity"] == 100000.0
    assert risk["open_positions_count"] == 1
    assert risk["trading_enabled"] is True
    assert strategy_positions[0]["symbol"] == "AAPL"
    assert signals["AAPL"]["signal"] == "BUY"
    assert auto_status["last_scanned_symbols"] == ["AAPL"]
    assert auto_status["open_positions_count"] == 1
    assert auto_status["last_order"]["symbol"] == "AAPL"


def test_auto_trade_enabled_starts_on_startup() -> None:
    settings_module._settings = build_settings(auto_trade_enabled=True, scan_interval_seconds=5)
    with TestClient(app) as client:
        status_response = client.get("/auto/status")
        start_response = client.post("/auto/start")
        stop_response = client.post("/auto/stop")

    assert status_response.status_code == 200
    assert status_response.json()["running"] is True
    assert start_response.status_code == 200
    assert "already running" in start_response.json()["message"].lower()
    assert stop_response.status_code == 200
    assert "stopped" in stop_response.json()["message"].lower()
