import os
from unittest.mock import patch

import pytest

from app.config.settings import Settings, is_placeholder_discord_webhook_url
from app.domain.models import AssetClass


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


def test_crypto_only_mode_uses_configured_crypto_symbols() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            crypto_only_mode=True,
            active_strategy="equity_momentum_breakout",
            default_symbols=["AAPL", "SPY"],
            included_symbols=["AAPL"],
            crypto_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],
        )

    assert settings.active_symbols == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert settings.active_crypto_symbols == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert settings.scan_symbol_allowlist == ["BTC/USD", "ETH/USD", "SOL/USD"]
    assert settings.active_asset_classes == ["crypto"]
    assert settings.primary_runtime_asset_class == AssetClass.CRYPTO
    assert settings.primary_runtime_strategy == "crypto_momentum_trend"


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
            discord_dedupe_ttl_seconds=90,
            broker_order_status_cache_path="custom-logs/broker_status.json",
            broker_order_status_suppress_startup_replay=True,
            broker_order_status_ignore_terminal_older_than_minutes=45,
            ml_enabled=True,
            entry_model_enabled=True,
            exit_model_enabled=True,
            ml_model_type="logistic_regression",
            ml_min_score_threshold=0.65,
            ml_exit_min_score=0.6,
            ml_min_train_rows=25,
            ml_retrain_enabled=True,
            ml_entry_min_auc=0.61,
            ml_entry_min_precision=0.58,
            ml_promotion_min_auc=0.61,
            ml_promotion_min_precision=0.58,
            ml_promotion_min_winrate_lift=0.03,
            ml_promotion_min_profit_factor=1.2,
            ml_promotion_max_drawdown=0.18,
            ml_promotion_min_expectancy=0.01,
            walk_forward_enabled=True,
            model_dir="custom-models",
            ml_current_model_path="custom-models/current.joblib",
            ml_candidate_model_path="custom-models/candidate.joblib",
            ml_entry_current_model_path="custom-models/current-entry.joblib",
            ml_entry_candidate_model_path="custom-models/candidate-entry.joblib",
            ml_exit_current_model_path="custom-models/current-exit.joblib",
            ml_exit_candidate_model_path="custom-models/candidate-exit.joblib",
            ml_registry_path="custom-models/registry.json",
            risk_per_trade_pct=0.02,
            max_symbol_allocation_pct=0.08,
            max_asset_class_allocation_pct={"equity": 0.25, "etf": 0.2, "crypto": 0.1, "option": 0.02},
            max_concurrent_positions=4,
            symbol_reentry_cooldown_minutes=15,
            enable_partial_exits=True,
            partial_take_profit_levels=[1.0, 2.0],
            partial_take_profit_fractions=[0.25, 1.0],
            break_even_after_r_multiple=1.2,
            trailing_stop_mode="atr",
            trailing_stop_atr_multiple=2.0,
            time_stop_bars=12,
            news_features_enabled=True,
            news_rss_enabled=True,
            news_llm_enabled=False,
            openai_model="gpt-4.1-nano",
            news_max_headlines_per_ticker=4,
            news_lookback_hours=12,
        )

    assert settings.log_dir == "custom-logs"
    assert settings.auto_trader_lock_path == "custom-logs/auto.lock"
    assert settings.discord_dedupe_ttl_seconds == 90
    assert settings.broker_order_status_cache_path == "custom-logs/broker_status.json"
    assert settings.ml_enabled is True
    assert settings.exit_model_enabled is True
    assert settings.ml_min_score_threshold == 0.65
    assert settings.ml_exit_min_score == 0.6
    assert settings.ml_retrain_enabled is True
    assert settings.ml_registry_path == "custom-models/registry.json"
    assert settings.risk_per_trade_pct == 0.02
    assert settings.max_symbol_allocation_pct == 0.08
    assert settings.max_asset_class_allocation_pct["equity"] == 0.25
    assert settings.symbol_reentry_cooldown_minutes == 15
    assert settings.partial_take_profit_levels == [1.0, 2.0]
    assert settings.partial_take_profit_fractions == [0.25, 1.0]
    assert settings.time_stop_bars == 12
    assert settings.news_features_enabled is True
    assert settings.news_rss_enabled is True
    assert settings.news_llm_enabled is False
    assert settings.openai_model == "gpt-4.1-nano"
    assert settings.news_lookback_hours == 12
