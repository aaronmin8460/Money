from __future__ import annotations

from app.config import settings as settings_module
from app.config.settings import Settings
from app.db.init_db import init_db
from app.db.models import BotRunHistory, FillRecord, Order, PositionSnapshotRecord, RiskEvent, SignalEvent
from app.db.session import SessionLocal
from app.domain.models import AssetClass
from app.services.local_state_reset import LocalStateResetOptions, reset_local_state
from app.services.runtime import get_runtime
from app.strategies.base import Signal, TradeSignal


def build_settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "broker_mode": "mock",
        "trading_enabled": True,
        "auto_trade_enabled": False,
        "default_symbols": ["AAPL"],
        "max_positions_total": 3,
        "max_notional_per_position": 10_000.0,
        "max_risk_per_trade": 0.05,
        "min_avg_volume": 1,
        "min_dollar_volume": 1,
        "min_price": 1,
    }
    values.update(overrides)
    return Settings(**values)


def test_local_reset_clears_runtime_state() -> None:
    settings_module._settings = build_settings()
    runtime = get_runtime()
    trader = runtime.get_auto_trader()
    runtime.execution_service.process_signal(
        TradeSignal(
            symbol="AAPL",
            signal=Signal.BUY,
            asset_class=AssetClass.EQUITY,
            strategy_name="test_strategy",
            price=100.0,
            stop_price=95.0,
            metrics={"avg_volume": 10_000, "dollar_volume": 1_000_000, "exchange": "MOCK"},
        )
    )
    runtime.risk_manager.mark_executed("AAPL", "test_strategy")
    runtime.risk_manager.guard_against("TSLA", "SELL", 1.0, 250.0, asset_class=AssetClass.EQUITY)
    trader._last_scanned_symbols = ["AAPL", "TSLA"]
    trader._last_signals = {"AAPL": {"signal": "BUY"}}
    trader._last_order = {"symbol": "AAPL"}
    trader._last_error = "example"
    assert runtime.tranche_state.snapshot()

    result = reset_local_state(LocalStateResetOptions(), runtime=runtime)

    assert result["paper_safe"] is True
    assert runtime.portfolio.positions == {}
    assert runtime.portfolio.risk_events == []
    assert runtime.portfolio.daily_baseline_equity == 100_000.0
    assert runtime.risk_manager.get_recent_rejections() == []
    assert runtime.risk_manager.get_active_cooldowns() == {
        "symbols": [],
        "strategies": [],
        "stop_out_symbols": [],
    }
    assert runtime.broker.list_orders() == []
    assert trader.get_status()["last_signals"] == {}
    assert trader.get_status()["last_order"] is None
    assert runtime.tranche_state.snapshot() == []
    assert result["tranche_state"] == []


def test_local_reset_with_db_wipe_clears_persisted_history() -> None:
    settings_module._settings = build_settings()
    runtime = get_runtime()
    init_db()
    with SessionLocal() as session:
        session.add(SignalEvent(symbol="RST", signal="BUY", price=10.0, reason="seed"))
        session.add(Order(symbol="RST", side="BUY", quantity=1.0, price=10.0, status="FILLED", is_dry_run=False))
        session.add(FillRecord(order_id="fill-1", symbol="RST", asset_class="equity", side="BUY", quantity=1.0, price=10.0))
        session.add(
            PositionSnapshotRecord(
                symbol="RST",
                asset_class="equity",
                quantity=1.0,
                entry_price=10.0,
                current_price=10.0,
                market_value=10.0,
                side="BUY",
            )
        )
        session.add(RiskEvent(symbol="RST", reason="seed", details="seed", is_blocked=True))
        session.add(BotRunHistory(run_type="manual", status="success", summary_json="{}"))
        session.commit()

    result = reset_local_state(LocalStateResetOptions(wipe_local_db=True), runtime=runtime)

    assert result["local_db_wiped"] is True
    with SessionLocal() as session:
        assert session.query(SignalEvent).count() == 0
        assert session.query(Order).count() == 0
        assert session.query(FillRecord).count() == 0
        assert session.query(PositionSnapshotRecord).count() == 0
        assert session.query(RiskEvent).count() == 0
        assert session.query(BotRunHistory).count() == 0
