import os
from unittest.mock import patch

from app.config.settings import Settings


def test_settings_defaults() -> None:
    """Test default settings without reading from .env."""
    # Create settings without env file to avoid .env leakage
    with patch.dict(os.environ, {}, clear=False):
        settings = Settings(
            _env_file=None,  # Disable env file loading
            broker_mode="paper",
            trading_enabled=False,
            max_risk_per_trade=0.01,
            default_symbols=["AAPL", "SPY"],
        )
    
    assert settings.broker_mode == "paper"
    assert settings.trading_enabled is False
    assert settings.max_risk_per_trade == 0.01
    assert "AAPL" in settings.default_symbols


def test_discord_notifications_require_webhook_when_enabled() -> None:
    with patch.dict(os.environ, {}, clear=False):
        try:
            Settings(
                _env_file=None,
                broker_mode="paper",
                trading_enabled=False,
                discord_notifications_enabled=True,
            )
        except ValueError as exc:
            assert "DISCORD_WEBHOOK_URL" in str(exc)
        else:
            raise AssertionError("Expected Discord notification settings validation to fail without a webhook URL.")
