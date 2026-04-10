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
