import tempfile
import uuid

from app.services import auto_trader as auto_trader_module
from app.config.settings import Settings
from app.domain.models import AssetClass, MarketSessionStatus, SessionState
from app.services.auto_trader import AutoTrader


def _test_lock_path() -> str:
    return f"{tempfile.gettempdir()}/money-auto-trader-{uuid.uuid4().hex}.lock"


def test_auto_trader_run_now_returns_result() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        default_symbols=["AAPL", "SPY"],
        max_positions=2,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    response = trader.run_now()

    assert response["success"] is True
    assert isinstance(response["results"], list)
    assert trader.get_status()["running"] is False


def test_auto_trader_prevents_duplicate_start() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.start() is False
    assert trader.stop() is True


def test_auto_trader_start_stop_send_notifications(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    calls: list[dict[str, str]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            calls.append(kwargs)
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.stop() is True
    assert calls[0]["event"] == "Paper auto-trader started"
    assert calls[0]["reason"] == "background loop started"
    assert calls[1]["event"] == "Paper auto-trader stopped"
    assert calls[1]["reason"] == "background loop stopped"


def test_scan_summary_sends_once_per_cycle(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    response = trader.run_now()

    assert response["success"] is True
    assert len(summary_calls) == 1
    assert "cycle_id" in summary_calls[0]


def test_scan_summary_dedupes_identical_overlap(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    evaluations = [
        {
            "symbol": "AAPL",
            "action": "skipped",
            "decision_rule": "market_closed_extended_hours_disabled",
            "decision_reason": "market closed",
        }
    ]
    counts = {"submitted": 0, "rejected": 0, "skipped": 1, "hold": 0}

    trader._notify_scan_summary(
        cycle_id="cycle-1",
        all_symbols=["AAPL"],
        evaluations=evaluations,
        results=[],
        outcome_counts=counts,
    )
    trader._notify_scan_summary(
        cycle_id="cycle-2",
        all_symbols=["AAPL"],
        evaluations=evaluations,
        results=[],
        outcome_counts=counts,
    )

    assert len(summary_calls) == 1


def test_run_now_overlap_does_not_emit_duplicate_summary(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        discord_notify_scan_summary=True,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    summary_calls: list[dict[str, object]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            return True

        def send_error_notification(self, **kwargs):
            return True

        def send_scan_summary_notification(self, **kwargs):
            summary_calls.append(kwargs)
            return True

        def send_broker_lifecycle_notification(self, **kwargs):
            return True

        def diagnostics(self):
            return {}

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    trader._cycle_guard.acquire()
    try:
        response = trader.run_now()
    finally:
        trader._cycle_guard.release()

    assert response["success"] is True
    assert response["results"] == []
    assert summary_calls == []


def test_run_symbol_now_market_closed_returns_skipped(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="mock",
        trading_enabled=False,
        allow_extended_hours=False,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
        auto_trader_lock_path=_test_lock_path(),
    )
    trader = AutoTrader(settings)

    def fake_session_status(_asset_class):
        return MarketSessionStatus(
            asset_class=AssetClass.EQUITY,
            is_open=False,
            session_state=SessionState.CLOSED,
            extended_hours=False,
            is_24_7=False,
        )

    monkeypatch.setattr(trader.market_data_service, "get_session_status", fake_session_status)
    result = trader.run_symbol_now("AAPL", AssetClass.EQUITY)

    assert result["action"] == "skipped"
    assert result["risk"]["rule"] == "market_closed_extended_hours_disabled"
