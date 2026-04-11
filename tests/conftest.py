from __future__ import annotations

import pytest

from app.config import settings as settings_module
from app.monitoring import discord_notifier as discord_notifier_module
from app.monitoring import outcome_logger as outcome_logger_module
from app.monitoring import trade_logger as trade_logger_module
from app.services import auto_trader as auto_trader_module
from app.services.runtime import close_runtime


@pytest.fixture(autouse=True)
def reset_singletons() -> None:
    close_runtime()
    settings_module._settings = None
    discord_notifier_module.reset_discord_notifier()
    trade_logger_module.reset_trade_logger()
    outcome_logger_module.reset_outcome_logger()
    auto_trader_module._auto_trader = None
    yield
    close_runtime()
    settings_module._settings = None
    discord_notifier_module.reset_discord_notifier()
    trade_logger_module.reset_trade_logger()
    outcome_logger_module.reset_outcome_logger()
    auto_trader_module._auto_trader = None
