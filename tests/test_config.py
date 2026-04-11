import os
from unittest.mock import patch

import pytest

from app.config.settings import Settings, is_placeholder_discord_webhook_url


def test_settings_defaults() -> None:
    """Test default settings without reading from .env."""
    # Create settings without env file to avoid .env leakage
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,  # Disable env file loading
            broker_mode="mock",
            trading_enabled=False,
            max_risk_per_trade=0.01,
            default_symbols=["AAPL", "SPY"],
        )
    
    assert settings.broker_mode == "mock"
    assert settings.trading_enabled is False
    assert settings.max_risk_per_trade == 0.01
    assert "AAPL" in settings.default_symbols


def test_active_symbols_uses_default_symbols_by_default() -> None:
    """Test that active_symbols uses default_symbols when included_symbols is empty."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            default_symbols=["AAPL", "SPY"],
            included_symbols=[],
        )
    assert settings.active_symbols == ["AAPL", "SPY"]


def test_active_symbols_uses_included_symbols_when_set() -> None:
    """Test that active_symbols uses included_symbols as backward-compatible alias."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            default_symbols=["AAPL", "SPY"],
            included_symbols=["BTC/USD", "ETH/USD"],
        )
    assert settings.active_symbols == ["BTC/USD", "ETH/USD"]


def test_active_strategy_defaults_to_equity_momentum_breakout() -> None:
    """Test that active_strategy defaults to the documented momentum breakout strategy."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
        )
    assert settings.active_strategy == "equity_momentum_breakout"
    assert settings.strategy_name == "equity_momentum_breakout"


def test_discord_notifications_require_webhook_when_enabled() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
            Settings(
                _env_file=None,
                broker_mode="mock",
                trading_enabled=False,
                discord_notifications_enabled=True,
            )


def test_placeholder_discord_webhook_url_is_detected() -> None:
    assert (
        is_placeholder_discord_webhook_url(
            "https://discord.com/api/webhooks/your_webhook_id/your_webhook_token"
        )
        is True
    )


def test_discord_notifications_reject_placeholder_webhook_url() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="real Discord webhook URL"):
            Settings(
                _env_file=None,
                broker_mode="mock",
                trading_enabled=False,
                discord_notifications_enabled=True,
                discord_webhook_url="https://discord.com/api/webhooks/your_webhook_id/your_webhook_token",
            )


def test_new_ml_and_news_settings_parse_correctly() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            log_dir="custom-logs",
            auto_trader_lock_path="custom-logs/auto.lock",
            ml_enabled=True,
            ml_model_type="logistic_regression",
            ml_min_score_threshold=0.65,
            ml_min_train_rows=25,
            ml_retrain_enabled=True,
            ml_promotion_min_auc=0.61,
            ml_promotion_min_precision=0.58,
            ml_promotion_min_winrate_lift=0.03,
            model_dir="custom-models",
            ml_current_model_path="custom-models/current.joblib",
            ml_candidate_model_path="custom-models/candidate.joblib",
            ml_registry_path="custom-models/registry.json",
            news_features_enabled=True,
            news_rss_enabled=True,
            news_llm_enabled=False,
            openai_model="gpt-4.1-nano",
            news_max_headlines_per_ticker=4,
            news_lookback_hours=12,
        )

    assert settings.log_dir == "custom-logs"
    assert settings.auto_trader_lock_path == "custom-logs/auto.lock"
    assert settings.ml_enabled is True
    assert settings.ml_min_score_threshold == 0.65
    assert settings.ml_retrain_enabled is True
    assert settings.ml_registry_path == "custom-models/registry.json"
    assert settings.news_features_enabled is True
    assert settings.news_rss_enabled is True
    assert settings.news_llm_enabled is False
    assert settings.openai_model == "gpt-4.1-nano"
    assert settings.news_lookback_hours == 12
