from app.config.settings import get_settings


def test_settings_defaults() -> None:
    settings = get_settings()
    assert settings.broker_mode == "paper"
    assert settings.trading_enabled is False
    assert settings.max_risk_per_trade == 0.01
    assert "AAPL" in settings.default_symbols
