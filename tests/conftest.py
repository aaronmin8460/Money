from __future__ import annotations

import pytest

from app.config import settings as settings_module
from app.db import session as db_session_module
from app.monitoring import discord_notifier as discord_notifier_module
from app.monitoring import outcome_logger as outcome_logger_module
from app.monitoring import trade_logger as trade_logger_module
from app.services import auto_trader as auto_trader_module
from app.services.runtime import close_runtime


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("BROKER_MODE", "mock")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("DISCORD_NOTIFICATIONS_ENABLED", "false")
    monkeypatch.setenv("API_ADMIN_TOKEN", "test-admin-token")

    close_runtime()
    settings_module._settings = None
    db_session_module.reset_engine(database_url)
    discord_notifier_module.reset_discord_notifier()
    trade_logger_module.reset_trade_logger()
    outcome_logger_module.reset_outcome_logger()
    auto_trader_module._auto_trader = None
    yield
    close_runtime()
    settings_module._settings = None
    db_session_module.reset_engine(database_url)
    discord_notifier_module.reset_discord_notifier()
    trade_logger_module.reset_trade_logger()
    outcome_logger_module.reset_outcome_logger()
    auto_trader_module._auto_trader = None
