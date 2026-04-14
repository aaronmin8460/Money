from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.domain.models import AssetClass, AssetMetadata
from app.portfolio.portfolio import Portfolio, Position
from app.risk.risk_manager import RiskManager
from app.services.runtime_safety import RuntimeSafetyManager
from app.services.tranche_state import TrancheStateStore


@dataclass
class FakeAccount:
    cash: float = 100000.0
    equity: float = 100000.0
    buying_power: float = 100000.0
    positions: int = 0
    mode: str = "mock"
    trading_enabled: bool = True


class FakeBroker:
    def __init__(self, *, positions: list[dict[str, object]] | None = None):
        self.positions = list(positions or [])

    def get_account(self) -> FakeAccount:
        return FakeAccount(positions=len(self.positions))

    def get_positions(self) -> list[dict[str, object]]:
        return list(self.positions)

    def get_asset(self, symbol: str, asset_class: AssetClass | str | None = None) -> AssetMetadata:
        resolved_class = asset_class if isinstance(asset_class, AssetClass) else AssetClass.EQUITY
        return AssetMetadata(
            symbol=symbol,
            name=symbol,
            asset_class=resolved_class,
            exchange="MOCK",
            tradable=True,
            fractionable=resolved_class == AssetClass.CRYPTO,
        )

    def list_assets(self) -> list[AssetMetadata]:
        return [
            AssetMetadata(
                symbol="AAPL",
                name="AAPL",
                asset_class=AssetClass.EQUITY,
                exchange="MOCK",
                tradable=True,
            )
        ]


def build_harness(**overrides: object) -> tuple[Settings, Portfolio, RiskManager, RuntimeSafetyManager, FakeBroker]:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=True,
        min_price=1.0,
        min_avg_volume=1.0,
        min_dollar_volume=1.0,
        **overrides,
    )
    portfolio = Portfolio()
    broker = FakeBroker()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    runtime_safety = RuntimeSafetyManager(
        settings=settings,
        broker=broker,
        portfolio=portfolio,
        tranche_state=TrancheStateStore(),
        risk_manager=risk_manager,
    )
    risk_manager.runtime_safety = runtime_safety
    return settings, portfolio, risk_manager, runtime_safety, broker


def test_consecutive_losing_exits_can_trigger_halt() -> None:
    settings, _portfolio, _risk_manager, runtime_safety, _broker = build_harness(
        halt_on_consecutive_losses=True,
        max_consecutive_losing_exits=3,
    )

    runtime_safety.record_exit_outcome(symbol="AAPL", order_intent="long_exit", trade_pnl=-10.0, exit_stage="stop")
    runtime_safety.record_exit_outcome(symbol="AAPL", order_intent="long_exit", trade_pnl=-12.0, exit_stage="trail")
    snapshot = runtime_safety.record_exit_outcome(
        symbol="AAPL",
        order_intent="long_exit",
        trade_pnl=-8.0,
        exit_stage="stop",
    )

    assert settings.max_consecutive_losing_exits == 3
    assert snapshot["halted"] is True
    assert snapshot["halt_rule"] == "consecutive_losing_exits"
    assert snapshot["consecutive_losing_exits"] == 3


def test_winning_exit_resets_consecutive_loss_counter() -> None:
    _settings, _portfolio, _risk_manager, runtime_safety, _broker = build_harness(
        halt_on_consecutive_losses=True,
        max_consecutive_losing_exits=3,
    )

    runtime_safety.record_exit_outcome(symbol="AAPL", order_intent="long_exit", trade_pnl=-5.0, exit_stage="stop")
    snapshot = runtime_safety.record_exit_outcome(
        symbol="AAPL",
        order_intent="long_exit",
        trade_pnl=15.0,
        exit_stage="tp1",
    )

    assert snapshot["consecutive_losing_exits"] == 0
    assert snapshot["halted"] is False


def test_halted_mode_blocks_new_entries_but_allows_risk_reducing_exits() -> None:
    _settings, portfolio, risk_manager, runtime_safety, _broker = build_harness()
    runtime_safety.manual_halt(operator_note="operator pause")

    entry_decision = risk_manager.evaluate_order("AAPL", "BUY", 1.0, 100.0, asset_class=AssetClass.EQUITY)

    portfolio.positions["AAPL"] = Position(
        symbol="AAPL",
        quantity=2.0,
        entry_price=100.0,
        side="BUY",
        current_price=95.0,
        asset_class=AssetClass.EQUITY,
    )
    exit_decision = risk_manager.evaluate_order(
        "AAPL",
        "SELL",
        1.0,
        95.0,
        order_intent="long_exit",
        reduce_only=True,
        asset_class=AssetClass.EQUITY,
    )

    assert entry_decision.approved is False
    assert entry_decision.rule == "manual_halt"
    assert exit_decision.approved is True
    assert exit_decision.rule == "approved"


def test_manual_resume_clears_halt_state_and_resets_counter() -> None:
    _settings, _portfolio, _risk_manager, runtime_safety, _broker = build_harness()
    runtime_safety.manual_halt(operator_note="manual pause")
    runtime_safety.record_exit_outcome(symbol="AAPL", order_intent="long_exit", trade_pnl=-5.0, exit_stage="stop")

    snapshot = runtime_safety.resume(
        operator_note="resume after review",
        reset_consecutive_losing_exits=True,
    )

    assert snapshot["halted"] is False
    assert snapshot["halt_reason"] is None
    assert snapshot["halt_rule"] is None
    assert snapshot["consecutive_losing_exits"] == 0
    assert snapshot["resumed_at"] is not None


def test_reconcile_mismatch_is_detected_and_surfaced() -> None:
    _settings, portfolio, risk_manager, runtime_safety, broker = build_harness(
        halt_on_reconcile_mismatch=False,
    )
    broker.positions = [
        {
            "symbol": "AAPL",
            "qty": 2.0,
            "avg_entry_price": 100.0,
            "current_price": 101.0,
            "side": "BUY",
            "asset_class": AssetClass.EQUITY.value,
            "exchange": "MOCK",
            "position_direction": "long",
        }
    ]

    snapshot = runtime_safety.reconcile(source="runtime_sync")

    assert snapshot["last_reconcile_status"] == "mismatch_detected"
    assert snapshot["mismatch_summary"]["broker_position_missing_locally"] == 1
    assert portfolio.get_position("AAPL") is not None
    assert risk_manager.get_runtime_snapshot()["runtime_safety"]["last_reconcile_status"] == "mismatch_detected"


def test_runtime_safety_sends_discord_alerts_for_halt_resume_and_reconcile(monkeypatch) -> None:
    _settings, _portfolio, _risk_manager, runtime_safety, broker = build_harness(
        halt_on_reconcile_mismatch=False,
    )
    calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr("app.services.runtime_safety.get_discord_notifier", lambda settings: StubNotifier())

    runtime_safety.manual_halt(operator_note="ops")
    runtime_safety.resume(operator_note="clear", reset_consecutive_losing_exits=True)
    broker.positions = [
        {
            "symbol": "AAPL",
            "qty": 1.0,
            "avg_entry_price": 100.0,
            "current_price": 102.0,
            "side": "BUY",
            "asset_class": AssetClass.EQUITY.value,
            "exchange": "MOCK",
            "position_direction": "long",
        }
    ]
    runtime_safety.reconcile(source="runtime_sync")

    assert [call["event"] for call in calls] == [
        "Bot halted by circuit breaker",
        "Bot resumed manually",
        "Reconcile mismatch detected",
    ]
