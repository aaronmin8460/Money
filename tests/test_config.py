import os
from pathlib import Path
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
    assert settings.dust_position_max_notional == 1.0
    assert settings.dust_position_max_qty_by_asset_class["crypto"] == 0.000001


def test_dust_position_settings_parse_json_object() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            dust_position_max_notional=0.5,
            dust_position_max_qty_by_asset_class='{"crypto": 0.0000025, "equity": 0.0}',
        )

    assert settings.dust_position_max_notional == 0.5
    assert settings.dust_position_max_qty_by_asset_class["crypto"] == 0.0000025
    assert settings.dust_position_max_qty_by_asset_class["equity"] == 0.0
    assert settings.dust_position_max_qty_by_asset_class["etf"] == 0.0


def test_asset_class_timeframe_defaults_are_available() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
        )

    assert settings.entry_timeframe_for_asset_class(AssetClass.EQUITY) == "15Min"
    assert settings.entry_timeframe_for_asset_class(AssetClass.CRYPTO) == "15Min"
    assert settings.regime_timeframe_for_asset_class(AssetClass.EQUITY) == "1D"
    assert settings.regime_timeframe_for_asset_class(AssetClass.CRYPTO) == "4H"
    assert settings.scanner_timeframe_for_asset_class(AssetClass.ETF) == "15Min"
    assert settings.lookback_bars_for_asset_class(AssetClass.CRYPTO) == 120
    assert settings.universe_prefilter_limit_for_asset_class(AssetClass.EQUITY) == 25
    assert settings.final_evaluation_limit_for_asset_class(AssetClass.EQUITY) == 10
    assert settings.market_data_provider_default == "composite"
    assert settings.equity_data_provider == "yfinance"
    assert settings.crypto_data_provider == "coingecko"
    assert settings.provider_rate_limits_per_minute["yfinance"] == 30


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


def test_trading_profile_defaults_to_conservative() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
        )

    assert settings.trading_profile == "conservative"
    assert settings.effective_trading_profile == "conservative"
    assert settings.effective_short_selling_enabled is False


def test_aggressive_profile_resolves_explicit_overrides() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            trading_profile="aggressive",
            aggressive_shorts_enabled=True,
            aggressive_extended_hours_enabled=True,
            aggressive_max_positions=6,
            aggressive_risk_per_trade_pct=0.015,
            aggressive_max_symbol_allocation_pct=0.13,
            aggressive_scan_interval_seconds_by_asset_class={"equity": 45, "crypto": 20},
            aggressive_final_evaluation_limit_by_asset_class={"equity": 30, "crypto": 26},
        )

    assert settings.effective_trading_profile == "aggressive"
    assert settings.effective_short_selling_enabled is True
    assert settings.effective_allow_extended_hours is True
    assert settings.effective_max_positions_total == 6
    assert settings.effective_risk_per_trade_pct == 0.015
    assert settings.effective_max_symbol_allocation_pct == 0.13
    assert settings.scan_interval_for_asset_class(AssetClass.EQUITY) == 45
    assert settings.final_evaluation_limit_for_asset_class(AssetClass.EQUITY) == 30
    assert "ema_crossover" in settings.candidate_strategies_for_asset_class(AssetClass.EQUITY)


def test_aggressive_mode_does_not_silently_activate() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            aggressive_shorts_enabled=True,
            aggressive_extended_hours_enabled=True,
        )

    assert settings.trading_profile == "conservative"
    assert settings.effective_trading_profile == "conservative"
    assert settings.effective_short_selling_enabled is False
    assert settings.effective_allow_extended_hours is False


def test_aggressive_profile_keeps_short_strategy_gated_without_short_opt_in() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            trading_profile="aggressive",
        )

    assert settings.effective_trading_profile == "aggressive"
    assert settings.effective_short_selling_enabled is False
    assert "ema_crossover" not in settings.candidate_strategies_for_asset_class(AssetClass.EQUITY)


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
            reuters_rss_urls='["https://example.com/reuters.xml"]',
            marketwatch_rss_urls='["https://example.com/marketwatch.xml"]',
            benzinga_rss_enabled=True,
            benzinga_rss_urls='["https://example.com/benzinga.xml"]',
            sec_company_tickers_cache_ttl_hours=12,
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
    assert settings.reuters_rss_urls == ["https://example.com/reuters.xml"]
    assert settings.marketwatch_rss_urls == ["https://example.com/marketwatch.xml"]
    assert settings.benzinga_rss_urls == ["https://example.com/benzinga.xml"]
    assert settings.sec_company_tickers_cache_ttl_hours == 12
    assert settings.enabled_news_sources == ["reuters", "marketwatch", "benzinga"]


def test_runtime_safety_settings_parse_correctly() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            halt_on_consecutive_losses=True,
            max_consecutive_losing_exits=4,
            halt_on_reconcile_mismatch=False,
            halt_on_startup_sync_failure=True,
        )

    assert settings.halt_on_consecutive_losses is True
    assert settings.max_consecutive_losing_exits == 4
    assert settings.halt_on_reconcile_mismatch is False
    assert settings.halt_on_startup_sync_failure is True


def test_runtime_safety_settings_reject_invalid_loss_threshold() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="MAX_CONSECUTIVE_LOSING_EXITS"):
            Settings(
                _env_file=None,
                broker_mode="mock",
                trading_enabled=False,
                max_consecutive_losing_exits=0,
            )


def test_api_admin_token_blank_is_normalized() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            broker_mode="mock",
            trading_enabled=False,
            api_admin_token="",
        )

    assert settings.api_admin_token is None


def test_env_example_uses_safe_defaults_and_placeholders() -> None:
    env_values: dict[str, str] = {}
    env_example_path = Path(__file__).resolve().parents[1] / ".env.example"
    contents = env_example_path.read_text(encoding="utf-8")

    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env_values[key] = value

    assert env_values["BROKER_MODE"] == "paper"
    assert env_values["TRADING_ENABLED"] == "false"
    assert env_values["AUTO_TRADE_ENABLED"] == "false"
    assert env_values["LIVE_TRADING_ENABLED"] == "false"
    assert env_values["DISCORD_NOTIFICATIONS_ENABLED"] == "false"
    assert env_values["ML_ENABLED"] == "false"
    assert env_values["ML_RETRAIN_ENABLED"] == "false"
    assert env_values["NEWS_FEATURES_ENABLED"] == "false"
    assert env_values["NEWS_RSS_ENABLED"] == "false"
    assert env_values["BENZINGA_RSS_ENABLED"] == "false"
    assert env_values["REUTERS_RSS_URLS"] == "[]"
    assert env_values["BENZINGA_RSS_URLS"] == "[]"
    assert env_values["SEC_COMPANY_TICKERS_CACHE_TTL_HOURS"] == "24"
    assert env_values["ALLOW_EXTENDED_HOURS"] == "false"
    assert env_values["HALT_ON_CONSECUTIVE_LOSSES"] == "true"
    assert env_values["MAX_CONSECUTIVE_LOSING_EXITS"] == "3"
    assert env_values["HALT_ON_RECONCILE_MISMATCH"] == "true"
    assert env_values["HALT_ON_STARTUP_SYNC_FAILURE"] == "true"
    assert env_values["ALPACA_API_KEY"] == ""
    assert env_values["ALPACA_SECRET_KEY"] == ""
    assert env_values["DISCORD_WEBHOOK_URL"] == ""
    assert env_values["API_ADMIN_TOKEN"] == ""
    assert env_values["MARKET_DATA_PROVIDER_DEFAULT"] == "composite"
    assert env_values["EQUITY_DATA_PROVIDER"] == "yfinance"
    assert env_values["ETF_DATA_PROVIDER"] == "yfinance"
    assert env_values["CRYPTO_DATA_PROVIDER"] == "coingecko"
    assert env_values["OPTION_DATA_PROVIDER"] == "yfinance"
    assert "www.benzinga.com/feeds/news" not in contents
    assert "1492154001083469857" not in contents
    assert "PK2JJVNJM44OQ7VJGHI5K5K2S5" not in contents
