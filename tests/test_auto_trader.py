from app.services import auto_trader as auto_trader_module
from app.config.settings import Settings
from app.services.auto_trader import AutoTrader


def test_auto_trader_run_now_returns_result() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=False,
        default_symbols=["AAPL", "SPY"],
        max_positions=2,
        scan_interval_seconds=1,
    )
    trader = AutoTrader(settings)
    response = trader.run_now()

    assert response["success"] is True
    assert isinstance(response["results"], list)
    assert trader.get_status()["running"] is False


def test_auto_trader_prevents_duplicate_start() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=False,
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
    )
    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.start() is False
    assert trader.stop() is True


def test_auto_trader_start_stop_send_notifications(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        broker_mode="paper",
        trading_enabled=False,
        discord_notifications_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test-id/test-token",
        default_symbols=["AAPL"],
        max_positions=1,
        scan_interval_seconds=1,
    )
    calls: list[dict[str, str]] = []

    class StubNotifier:
        def send_system_notification(self, **kwargs):
            calls.append(kwargs)
            return True

        def send_error_notification(self, **kwargs):
            return True

    monkeypatch.setattr(auto_trader_module, "get_discord_notifier", lambda settings: StubNotifier())

    trader = AutoTrader(settings)
    assert trader.start() is True
    assert trader.stop() is True
    assert calls[0]["event"] == "Bot started"
    assert calls[0]["reason"] == "background loop started"
    assert calls[1]["event"] == "Bot stopped"
    assert calls[1]["reason"] == "background loop stopped"
