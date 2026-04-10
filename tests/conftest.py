from __future__ import annotations

import pytest

from app.config import settings as settings_module
from app.services import auto_trader as auto_trader_module
from app.services.runtime import close_runtime


@pytest.fixture(autouse=True)
def reset_singletons() -> None:
    close_runtime()
    settings_module._settings = None
    auto_trader_module._auto_trader = None
    yield
    close_runtime()
    settings_module._settings = None
    auto_trader_module._auto_trader = None
