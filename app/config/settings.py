from __future__ import annotations

import json
from typing import Annotated, Any, List

from pydantic import AnyHttpUrl, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from app.config.trading_profile import (
    DEFAULT_AGGRESSIVE_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS,
    DEFAULT_AGGRESSIVE_SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS,
    DEFAULT_AGGRESSIVE_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS,
    ResolvedTradingProfile,
    resolve_trading_profile,
)
from app.domain.models import AssetClass


DEFAULT_ENTRY_TIMEFRAME_BY_ASSET_CLASS: dict[str, str] = {
    AssetClass.EQUITY.value: "15Min",
    AssetClass.ETF.value: "15Min",
    AssetClass.CRYPTO.value: "15Min",
    AssetClass.OPTION.value: "1D",
}
DEFAULT_REGIME_TIMEFRAME_BY_ASSET_CLASS: dict[str, str] = {
    AssetClass.EQUITY.value: "1D",
    AssetClass.ETF.value: "1D",
    AssetClass.CRYPTO.value: "4H",
    AssetClass.OPTION.value: "1D",
}
DEFAULT_SCANNER_TIMEFRAME_BY_ASSET_CLASS: dict[str, str] = {
    AssetClass.EQUITY.value: "15Min",
    AssetClass.ETF.value: "15Min",
    AssetClass.CRYPTO.value: "15Min",
    AssetClass.OPTION.value: "1D",
}
DEFAULT_LOOKBACK_BARS_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 90,
    AssetClass.ETF.value: 90,
    AssetClass.CRYPTO.value: 120,
    AssetClass.OPTION.value: 60,
}
DEFAULT_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 25,
    AssetClass.ETF.value: 20,
    AssetClass.CRYPTO.value: 20,
    AssetClass.OPTION.value: 10,
}
DEFAULT_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS: dict[str, int] = {
    AssetClass.EQUITY.value: 10,
    AssetClass.ETF.value: 8,
    AssetClass.CRYPTO.value: 8,
    AssetClass.OPTION.value: 5,
}


def _parse_json_list(value: str | list[str] | None, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a JSON array or list.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array.")
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def _parse_string_list(
    value: str | list[str] | None,
    field_name: str,
    *,
    upper: bool = False,
) -> list[str]:
    if value is None:
        return []
    parsed_values: list[str]
    if isinstance(value, list):
        parsed_values = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
            if not isinstance(parsed, list):
                raise ValueError(f"{field_name} must be a JSON array.")
            parsed_values = [str(item).strip() for item in parsed if str(item).strip()]
        else:
            parsed_values = [part.strip() for part in stripped.split(",") if part.strip()]
    else:
        raise ValueError(f"{field_name} must be a JSON array, comma-separated string, or list.")
    if upper:
        return [item.upper() for item in parsed_values]
    return parsed_values


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _looks_like_crypto_symbol(symbol: str) -> bool:
    normalized = str(symbol).strip().upper()
    if not normalized:
        return False
    if "/" in normalized:
        base, quote = normalized.split("/", 1)
        return bool(base) and quote in {"USD", "USDT", "USDC", "BTC", "ETH"}
    return normalized.endswith(("USD", "USDT", "USDC")) and len(normalized) > 3


def _filter_crypto_symbols(symbols: list[str]) -> list[str]:
    return _dedupe_symbols([symbol for symbol in symbols if _looks_like_crypto_symbol(symbol)])


def _parse_json_object(value: str | dict[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a JSON object.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return parsed


def _parse_numeric_list(value: str | list[float] | list[str] | None, field_name: str) -> list[float]:
    if value is None:
        return []
    if isinstance(value, list):
        parsed_values = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed_values = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field_name} must be valid JSON when provided as an array: {exc}") from exc
            if not isinstance(parsed_values, list):
                raise ValueError(f"{field_name} JSON payload must be a list.")
        else:
            parsed_values = [part.strip() for part in stripped.split(",") if part.strip()]
    else:
        raise ValueError(f"{field_name} must be a comma-separated string or list of numbers.")

    numbers: list[float] = []
    for item in parsed_values:
        try:
            numbers.append(float(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} contains a non-numeric value: {item!r}") from exc
    return numbers


def is_placeholder_discord_webhook_url(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return "your_webhook_id" in lowered or "your_webhook_token" in lowered


class Settings(BaseSettings):
    app_env: str = Field("development", env="APP_ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    database_url: str = Field("sqlite:///./trading.db", env="DATABASE_URL")
    log_dir: str = Field("logs", env="LOG_DIR")
    api_admin_token: str | None = Field(None, env="API_ADMIN_TOKEN")
    broker_mode: str = Field("paper", env="BROKER_MODE")
    alpaca_api_key: str | None = Field(None, env="ALPACA_API_KEY")
    alpaca_secret_key: str | None = Field(None, env="ALPACA_SECRET_KEY")
    alpaca_base_url: AnyHttpUrl = Field("https://paper-api.alpaca.markets", env="ALPACA_BASE_URL")
    alpaca_crypto_location: str = Field("us", env="ALPACA_CRYPTO_LOCATION")
    trading_enabled: bool = Field(False, env="TRADING_ENABLED")
    live_trading_enabled: bool = Field(False, env="LIVE_TRADING_ENABLED")
    live_trading_ack: str | None = Field(None, env="LIVE_TRADING_ACK")
    trading_profile: str = Field("conservative", env="TRADING_PROFILE")
    aggressive_mode_enabled: bool = Field(False, env="AGGRESSIVE_MODE_ENABLED")
    aggressive_profile_version: str = Field("v1", env="AGGRESSIVE_PROFILE_VERSION")
    discord_notifications_enabled: bool = Field(False, env="DISCORD_NOTIFICATIONS_ENABLED")
    discord_webhook_url: AnyHttpUrl | None = Field(None, env="DISCORD_WEBHOOK_URL")
    discord_notify_dry_runs: bool = Field(False, env="DISCORD_NOTIFY_DRY_RUNS")
    discord_notify_rejections: bool = Field(True, env="DISCORD_NOTIFY_REJECTIONS")
    discord_notify_errors: bool = Field(True, env="DISCORD_NOTIFY_ERRORS")
    discord_notify_start_stop: bool = Field(True, env="DISCORD_NOTIFY_START_STOP")
    discord_dedupe_ttl_seconds: float = Field(45.0, env="DISCORD_DEDUPE_TTL_SECONDS")
    broker_order_status_cache_path: str = Field(
        "logs/broker_order_status_memory.json",
        env="BROKER_ORDER_STATUS_CACHE_PATH",
    )
    broker_order_status_suppress_startup_replay: bool = Field(
        True,
        env="BROKER_ORDER_STATUS_SUPPRESS_STARTUP_REPLAY",
    )
    broker_order_status_ignore_terminal_older_than_minutes: int = Field(
        180,
        env="BROKER_ORDER_STATUS_IGNORE_TERMINAL_OLDER_THAN_MINUTES",
    )
    max_risk_per_trade: float = Field(0.01, env="MAX_RISK_PER_TRADE")
    risk_per_trade_pct: float = Field(0.01, env="RISK_PER_TRADE_PCT")
    max_daily_loss: float = Field(2_000.0, env="MAX_DAILY_LOSS")
    max_daily_loss_pct: float = Field(0.02, env="MAX_DAILY_LOSS_PCT")
    max_drawdown_pct: float = Field(0.10, env="MAX_DRAWDOWN_PCT")
    max_positions: int = Field(3, env="MAX_POSITIONS")
    max_positions_total: int = Field(3, env="MAX_POSITIONS_TOTAL")
    max_concurrent_positions: int = Field(3, env="MAX_CONCURRENT_POSITIONS")
    max_positions_per_asset_class: dict[str, int] = Field(
        default_factory=lambda: {
            AssetClass.EQUITY.value: 3,
            AssetClass.ETF.value: 3,
            AssetClass.CRYPTO.value: 2,
            AssetClass.OPTION.value: 1,
        },
        env="MAX_POSITIONS_PER_ASSET_CLASS",
    )
    default_timeframe: str = Field("1D", env="DEFAULT_TIMEFRAME")
    entry_timeframe_by_asset_class: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_ENTRY_TIMEFRAME_BY_ASSET_CLASS),
        env="ENTRY_TIMEFRAME_BY_ASSET_CLASS",
    )
    regime_timeframe_by_asset_class: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_REGIME_TIMEFRAME_BY_ASSET_CLASS),
        env="REGIME_TIMEFRAME_BY_ASSET_CLASS",
    )
    scanner_timeframe_by_asset_class: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_SCANNER_TIMEFRAME_BY_ASSET_CLASS),
        env="SCANNER_TIMEFRAME_BY_ASSET_CLASS",
    )
    lookback_bars_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_LOOKBACK_BARS_BY_ASSET_CLASS),
        env="LOOKBACK_BARS_BY_ASSET_CLASS",
    )
    default_symbols: List[str] = Field(default_factory=lambda: ["AAPL", "SPY"], env="DEFAULT_SYMBOLS")
    active_strategy: str = Field("equity_momentum_breakout", env="ACTIVE_STRATEGY")
    active_strategy_by_asset_class: dict[str, str] = Field(default_factory=dict, env="ACTIVE_STRATEGY_BY_ASSET_CLASS")
    strategy_name: str | None = Field(None, env="STRATEGY_NAME", exclude=True, repr=False)
    auto_trade_enabled: bool = Field(False, env="AUTO_TRADE_ENABLED")
    scan_interval_seconds: int = Field(60, env="SCAN_INTERVAL_SECONDS")
    scan_interval_seconds_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: {
            AssetClass.EQUITY.value: 120,
            AssetClass.ETF.value: 120,
            AssetClass.CRYPTO.value: 60,
        },
        env="SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS",
    )
    universe_prefilter_limit_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS),
        env="UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS",
    )
    final_evaluation_limit_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS),
        env="FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS",
    )
    alpaca_data_base_url: AnyHttpUrl = Field("https://data.alpaca.markets", env="ALPACA_DATA_BASE_URL")
    market_data_provider_default: str = Field("composite", env="MARKET_DATA_PROVIDER_DEFAULT")
    equity_data_provider: str = Field("yfinance", env="EQUITY_DATA_PROVIDER")
    etf_data_provider: str = Field("yfinance", env="ETF_DATA_PROVIDER")
    crypto_data_provider: str = Field("coingecko", env="CRYPTO_DATA_PROVIDER")
    option_data_provider: str = Field("yfinance", env="OPTION_DATA_PROVIDER")
    market_data_fallback_providers: list[str] = Field(default_factory=lambda: ["alpaca"], env="MARKET_DATA_FALLBACK_PROVIDERS")
    provider_rate_limits_per_minute: dict[str, int] = Field(
        default_factory=lambda: {"alpaca": 120, "yfinance": 30, "coingecko": 25, "tradier": 60},
        env="PROVIDER_RATE_LIMITS_PER_MINUTE",
    )
    snapshot_cache_ttl_seconds: float = Field(5.0, env="SNAPSHOT_CACHE_TTL_SECONDS")
    intraday_bars_cache_ttl_seconds: float = Field(30.0, env="INTRADAY_BARS_CACHE_TTL_SECONDS")
    daily_bars_cache_ttl_seconds: float = Field(300.0, env="DAILY_BARS_CACHE_TTL_SECONDS")
    option_chain_cache_ttl_seconds: float = Field(60.0, env="OPTION_CHAIN_CACHE_TTL_SECONDS")
    market_data_max_retries: int = Field(2, env="MARKET_DATA_MAX_RETRIES")
    market_data_backoff_base_seconds: float = Field(1.0, env="MARKET_DATA_BACKOFF_BASE_SECONDS")
    market_data_backoff_max_seconds: float = Field(30.0, env="MARKET_DATA_BACKOFF_MAX_SECONDS")
    coingecko_base_url: AnyHttpUrl = Field("https://api.coingecko.com", env="COINGECKO_BASE_URL")
    coingecko_api_key: str | None = Field(None, env="COINGECKO_API_KEY")
    tradier_api_token: str | None = Field(None, env="TRADIER_API_TOKEN")
    tradier_base_url: AnyHttpUrl = Field("https://api.tradier.com", env="TRADIER_BASE_URL")
    max_position_notional: float = Field(10000.0, env="MAX_POSITION_NOTIONAL")
    position_notional_buffer_pct: float = Field(0.995, env="POSITION_NOTIONAL_BUFFER_PCT")
    max_symbol_allocation_pct: float = Field(0.10, env="MAX_SYMBOL_ALLOCATION_PCT")
    max_asset_class_allocation_pct: dict[str, float] = Field(
        default_factory=lambda: {
            AssetClass.EQUITY.value: 0.35,
            AssetClass.ETF.value: 0.35,
            AssetClass.CRYPTO.value: 0.15,
            AssetClass.OPTION.value: 0.05,
        },
        env="MAX_ASSET_CLASS_ALLOCATION_PCT",
    )
    entry_tranches: int = Field(3, env="ENTRY_TRANCHES")
    entry_tranche_weights: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [0.4, 0.3, 0.3],
        env="ENTRY_TRANCHE_WEIGHTS",
    )
    scale_in_mode: str = Field("confirmation", env="SCALE_IN_MODE")
    min_bars_between_tranches: int = Field(1, env="MIN_BARS_BETWEEN_TRANCHES")
    minutes_between_tranches: int = Field(5, env="MINUTES_BETWEEN_TRANCHES")
    add_on_favorable_move_pct: float = Field(0.5, env="ADD_ON_FAVORABLE_MOVE_PCT")
    allow_average_down: bool = Field(False, env="ALLOW_AVERAGE_DOWN")
    max_total_exposure: float = Field(50_000.0, env="MAX_TOTAL_EXPOSURE")
    max_notional_per_position: float = Field(10_000.0, env="MAX_NOTIONAL_PER_POSITION")
    max_notional_per_asset_class: dict[str, float] = Field(
        default_factory=lambda: {
            AssetClass.EQUITY.value: 20_000.0,
            AssetClass.ETF.value: 20_000.0,
            AssetClass.CRYPTO.value: 10_000.0,
            AssetClass.OPTION.value: 2_500.0,
        },
        env="MAX_NOTIONAL_PER_ASSET_CLASS",
    )
    dust_position_max_notional: float = Field(1.00, env="DUST_POSITION_MAX_NOTIONAL")
    dust_position_max_qty_by_asset_class: dict[str, float] = Field(
        default_factory=lambda: {
            AssetClass.CRYPTO.value: 0.000001,
            AssetClass.EQUITY.value: 0.0,
            AssetClass.ETF.value: 0.0,
            AssetClass.OPTION.value: 0.0,
        },
        env="DUST_POSITION_MAX_QTY_BY_ASSET_CLASS",
    )
    max_correlated_positions: int = Field(2, env="MAX_CORRELATED_POSITIONS")
    cooldown_seconds_per_symbol: int = Field(300, env="COOLDOWN_SECONDS_PER_SYMBOL")
    cooldown_seconds_per_strategy: int = Field(180, env="COOLDOWN_SECONDS_PER_STRATEGY")
    symbol_reentry_cooldown_minutes: int = Field(0, env="SYMBOL_REENTRY_COOLDOWN_MINUTES")
    take_profit_pct: float = Field(0.05, env="TAKE_PROFIT_PCT")
    stop_loss_atr_multiplier: float = Field(2.0, env="STOP_LOSS_ATR_MULTIPLIER")
    enable_partial_exits: bool = Field(True, env="ENABLE_PARTIAL_EXITS")
    partial_take_profit_levels: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [1.0, 2.0],
        env="PARTIAL_TAKE_PROFIT_LEVELS",
    )
    partial_take_profit_fractions: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [0.5, 1.0],
        env="PARTIAL_TAKE_PROFIT_FRACTIONS",
    )
    break_even_after_r_multiple: float = Field(1.0, env="BREAK_EVEN_AFTER_R_MULTIPLE")
    trailing_stop_mode: str = Field("atr", env="TRAILING_STOP_MODE")
    trailing_stop_atr_multiple: float = Field(2.5, env="TRAILING_STOP_ATR_MULTIPLE")
    time_stop_bars: int = Field(20, env="TIME_STOP_BARS")
    allow_extended_hours: bool = Field(False, env="ALLOW_EXTENDED_HOURS")
    kill_switch_enabled: bool = Field(False, env="KILL_SWITCH_ENABLED")
    short_selling_enabled: bool = Field(False, env="SHORT_SELLING_ENABLED")
    require_easy_to_borrow_for_shorts: bool = Field(True, env="REQUIRE_EASY_TO_BORROW_FOR_SHORTS")
    require_marginable_for_shorts: bool = Field(True, env="REQUIRE_MARGINABLE_FOR_SHORTS")
    universe_scan_enabled: bool = Field(True, env="UNIVERSE_SCAN_ENABLED")
    universe_refresh_minutes: int = Field(60, env="UNIVERSE_REFRESH_MINUTES")
    enabled_asset_classes: list[str] = Field(
        default_factory=lambda: [
            AssetClass.EQUITY.value,
            AssetClass.ETF.value,
            AssetClass.CRYPTO.value,
        ],
        env="ENABLED_ASSET_CLASSES",
    )
    crypto_only_mode: bool = Field(False, env="CRYPTO_ONLY_MODE")
    equity_trading_enabled: bool = Field(True, env="EQUITY_TRADING_ENABLED")
    etf_trading_enabled: bool = Field(True, env="ETF_TRADING_ENABLED")
    crypto_trading_enabled: bool = Field(True, env="CRYPTO_TRADING_ENABLED")
    option_trading_enabled: bool = Field(False, env="OPTION_TRADING_ENABLED")
    watchlists: dict[str, list[str]] = Field(default_factory=dict, env="WATCHLISTS")
    excluded_symbols: list[str] = Field(default_factory=list, env="EXCLUDED_SYMBOLS")
    included_symbols: list[str] = Field(default_factory=list, env="INCLUDED_SYMBOLS")
    min_dollar_volume: float = Field(100_000.0, env="MIN_DOLLAR_VOLUME")
    min_price: float = Field(5.0, env="MIN_PRICE")
    min_avg_volume: float = Field(1_000.0, env="MIN_AVG_VOLUME")
    max_spread_pct: float = Field(0.02, env="MAX_SPREAD_PCT")
    data_stale_after_seconds: int = Field(900, env="DATA_STALE_AFTER_SECONDS")
    quote_stale_after_seconds: int = Field(30, env="QUOTE_STALE_AFTER_SECONDS")
    scanner_limit_per_asset_class: int = Field(50, env="SCANNER_LIMIT_PER_ASSET_CLASS")
    strategy_switches: dict[str, bool] = Field(default_factory=dict, env="STRATEGY_SWITCHES")
    discord_notify_holds_manual: bool = Field(True, env="DISCORD_NOTIFY_HOLDS_MANUAL")
    discord_notify_scan_summary: bool = Field(False, env="DISCORD_NOTIFY_SCAN_SUMMARY")
    discord_notify_crypto: bool = Field(True, env="DISCORD_NOTIFY_CRYPTO")
    discord_timezone: str = Field("America/Indiana/Indianapolis", env="DISCORD_TIMEZONE")
    auto_trader_lock_path: str = Field("logs/auto_trader.lock", env="AUTO_TRADER_LOCK_PATH")
    halt_on_consecutive_losses: bool = Field(True, env="HALT_ON_CONSECUTIVE_LOSSES")
    max_consecutive_losing_exits: int = Field(3, env="MAX_CONSECUTIVE_LOSING_EXITS")
    halt_on_reconcile_mismatch: bool = Field(True, env="HALT_ON_RECONCILE_MISMATCH")
    halt_on_startup_sync_failure: bool = Field(True, env="HALT_ON_STARTUP_SYNC_FAILURE")
    scan_universe_mode: str = Field("major", env="SCAN_UNIVERSE_MODE")
    major_equity_symbols: list[str] = Field(
        default_factory=lambda: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "IWM"],
        env="MAJOR_EQUITY_SYMBOLS",
    )
    major_crypto_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD"],
        env="MAJOR_CRYPTO_SYMBOLS",
    )
    crypto_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "DOGE/USD", "LINK/USD", "LTC/USD"],
        env="CRYPTO_SYMBOLS",
    )
    prefer_primary_crypto_quotes: bool = Field(True, env="PREFER_PRIMARY_CRYPTO_QUOTES")
    ml_enabled: bool = Field(False, env="ML_ENABLED")
    entry_model_enabled: bool = Field(True, env="ENTRY_MODEL_ENABLED")
    exit_model_enabled: bool = Field(False, env="EXIT_MODEL_ENABLED")
    ml_model_type: str = Field("logistic_regression", env="ML_MODEL_TYPE")
    ml_min_score_threshold: float = Field(0.55, env="ML_MIN_SCORE_THRESHOLD")
    ml_exit_min_score: float = Field(0.55, env="ML_EXIT_MIN_SCORE")
    ml_min_train_rows: int = Field(50, env="ML_MIN_TRAIN_ROWS")
    ml_retrain_enabled: bool = Field(False, env="ML_RETRAIN_ENABLED")
    ml_entry_min_auc: float = Field(0.55, env="ML_ENTRY_MIN_AUC")
    ml_entry_min_precision: float = Field(0.50, env="ML_ENTRY_MIN_PRECISION")
    ml_promotion_min_auc: float = Field(0.55, env="ML_PROMOTION_MIN_AUC")
    ml_promotion_min_precision: float = Field(0.50, env="ML_PROMOTION_MIN_PRECISION")
    ml_promotion_min_winrate_lift: float = Field(0.00, env="ML_PROMOTION_MIN_WINRATE_LIFT")
    ml_promotion_min_profit_factor: float = Field(1.05, env="ML_PROMOTION_MIN_PROFIT_FACTOR")
    ml_promotion_max_drawdown: float = Field(0.20, env="ML_PROMOTION_MAX_DRAWDOWN")
    ml_promotion_min_expectancy: float = Field(0.0, env="ML_PROMOTION_MIN_EXPECTANCY")
    walk_forward_enabled: bool = Field(True, env="WALK_FORWARD_ENABLED")
    model_dir: str = Field("models", env="MODEL_DIR")
    ml_current_model_path: str = Field("models/current_model.joblib", env="ML_CURRENT_MODEL_PATH")
    ml_candidate_model_path: str = Field("models/candidate_model.joblib", env="ML_CANDIDATE_MODEL_PATH")
    ml_entry_current_model_path: str = Field("models/current_model.joblib", env="ML_ENTRY_CURRENT_MODEL_PATH")
    ml_entry_candidate_model_path: str = Field("models/candidate_model.joblib", env="ML_ENTRY_CANDIDATE_MODEL_PATH")
    ml_exit_current_model_path: str = Field("models/current_exit_model.joblib", env="ML_EXIT_CURRENT_MODEL_PATH")
    ml_exit_candidate_model_path: str = Field("models/candidate_exit_model.joblib", env="ML_EXIT_CANDIDATE_MODEL_PATH")
    ml_registry_path: str = Field("models/registry.json", env="ML_REGISTRY_PATH")
    news_features_enabled: bool = Field(False, env="NEWS_FEATURES_ENABLED")
    news_rss_enabled: bool = Field(False, env="NEWS_RSS_ENABLED")
    news_llm_enabled: bool = Field(True, env="NEWS_LLM_ENABLED")
    openai_api_key: str | None = Field(None, env="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4.1-nano", env="OPENAI_MODEL")
    news_max_headlines_per_ticker: int = Field(8, env="NEWS_MAX_HEADLINES_PER_TICKER")
    news_lookback_hours: int = Field(24, env="NEWS_LOOKBACK_HOURS")
    news_source_ids: list[str] = Field(default_factory=list, env="NEWS_SOURCE_IDS")
    benzinga_rss_enabled: bool = Field(False, env="BENZINGA_RSS_ENABLED")
    benzinga_rss_urls: list[str] = Field(
        default_factory=lambda: ["https://www.benzinga.com/feeds/news"],
        env="BENZINGA_RSS_URLS",
    )
    sec_rss_enabled: bool = Field(False, env="SEC_RSS_ENABLED")
    sec_rss_urls: list[str] = Field(
        default_factory=lambda: [
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-k&count=100&output=atom",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-q&count=100&output=atom",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-k&count=100&output=atom",
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13d&count=100&output=atom",
        ],
        env="SEC_RSS_URLS",
    )
    sec_user_agent: str = Field("MoneyBot/1.0 (paper-safe research; contact unset)", env="SEC_USER_AGENT")
    news_fetch_timeout_seconds: float = Field(10.0, env="NEWS_FETCH_TIMEOUT_SECONDS")
    news_fetch_retry_count: int = Field(2, env="NEWS_FETCH_RETRY_COUNT")
    news_fetch_backoff_seconds: float = Field(1.5, env="NEWS_FETCH_BACKOFF_SECONDS")
    news_dedupe_window_minutes: int = Field(45, env="NEWS_DEDUPE_WINDOW_MINUTES")
    news_source_weights: dict[str, float] = Field(default_factory=dict, env="NEWS_SOURCE_WEIGHTS")
    news_enable_source_diversity_features: bool = Field(True, env="NEWS_ENABLE_SOURCE_DIVERSITY_FEATURES")
    rate_limit_enabled: bool = Field(False, env="RATE_LIMIT_ENABLED")
    rate_limit_default: str = Field("120/minute", env="RATE_LIMIT_DEFAULT")
    rate_limit_storage_uri: str = Field("memory://", env="RATE_LIMIT_STORAGE_URI")
    rate_limit_headers_enabled: bool = Field(True, env="RATE_LIMIT_HEADERS_ENABLED")
    rate_limit_scanner: str = Field("12/minute", env="RATE_LIMIT_SCANNER")
    rate_limit_admin: str = Field("10/minute", env="RATE_LIMIT_ADMIN")
    rate_limit_market: str = Field("60/minute", env="RATE_LIMIT_MARKET")
    rate_limit_signals: str = Field("30/minute", env="RATE_LIMIT_SIGNALS")
    rate_limit_health_exempt: bool = Field(True, env="RATE_LIMIT_HEALTH_EXEMPT")
    aggressive_entry_threshold_adjustment: float = Field(-0.08, env="AGGRESSIVE_ENTRY_THRESHOLD_ADJUSTMENT")
    aggressive_max_positions: int = Field(5, env="AGGRESSIVE_MAX_POSITIONS")
    aggressive_max_positions_per_asset_class: dict[str, int] = Field(
        default_factory=lambda: {
            AssetClass.EQUITY.value: 4,
            AssetClass.ETF.value: 4,
            AssetClass.CRYPTO.value: 3,
            AssetClass.OPTION.value: 1,
        },
        env="AGGRESSIVE_MAX_POSITIONS_PER_ASSET_CLASS",
    )
    aggressive_risk_per_trade_pct: float = Field(0.0125, env="AGGRESSIVE_RISK_PER_TRADE_PCT")
    aggressive_max_symbol_allocation_pct: float = Field(0.12, env="AGGRESSIVE_MAX_SYMBOL_ALLOCATION_PCT")
    aggressive_scan_interval_seconds_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_AGGRESSIVE_SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS),
        env="AGGRESSIVE_SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS",
    )
    aggressive_universe_prefilter_limit_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_AGGRESSIVE_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS),
        env="AGGRESSIVE_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS",
    )
    aggressive_final_evaluation_limit_by_asset_class: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_AGGRESSIVE_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS),
        env="AGGRESSIVE_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS",
    )
    aggressive_news_catalyst_weight: float = Field(0.35, env="AGGRESSIVE_NEWS_CATALYST_WEIGHT")
    aggressive_shorts_enabled: bool = Field(False, env="AGGRESSIVE_SHORTS_ENABLED")
    aggressive_extended_hours_enabled: bool = Field(False, env="AGGRESSIVE_EXTENDED_HOURS_ENABLED")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def is_paper_mode(self) -> bool:
        return self.broker_mode.lower() == "paper"

    @property
    def is_mock_mode(self) -> bool:
        return self.broker_mode.lower() == "mock"

    @property
    def is_alpaca_mode(self) -> bool:
        return self.broker_mode.lower() == "paper"

    @property
    def is_simulated_mode(self) -> bool:
        return self.broker_mode.lower() in {"paper", "mock"}

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def is_live_enabled(self) -> bool:
        return self.trading_enabled and self.is_alpaca_mode and self.live_trading_enabled

    @property
    def broker_backend(self) -> str:
        return "alpaca_paper" if self.is_alpaca_mode else "local_mock"

    @property
    def order_submission_mode(self) -> str:
        if not self.trading_enabled:
            return "dry_run"
        if self.live_trading_enabled:
            return "live_order_submission"
        if self.is_paper_mode:
            return "paper_order_submission"
        return "mock_order_submission"

    @property
    def effective_max_position_notional(self) -> float:
        return float(self.max_position_notional * self.position_notional_buffer_pct)

    @property
    def enabled_asset_class_set(self) -> set[AssetClass]:
        if self.crypto_only_mode:
            return {AssetClass.CRYPTO}
        raw = {item.lower() for item in self.enabled_asset_classes}
        allowed = set()
        for value in raw:
            try:
                allowed.add(AssetClass(value))
            except ValueError:
                continue
        if self.equity_trading_enabled:
            allowed.add(AssetClass.EQUITY)
        if self.etf_trading_enabled:
            allowed.add(AssetClass.ETF)
        if self.crypto_trading_enabled:
            allowed.add(AssetClass.CRYPTO)
        if self.option_trading_enabled:
            allowed.add(AssetClass.OPTION)
        return allowed

    @property
    def watchlist_symbols(self) -> list[str]:
        values: list[str] = []
        for symbols in self.watchlists.values():
            values.extend(symbols)
        return _dedupe_symbols([symbol for symbol in values if str(symbol).strip()])

    @property
    def manual_symbols(self) -> list[str]:
        return self.default_symbols or self.watchlist_symbols

    @property
    def active_crypto_symbols(self) -> list[str]:
        candidate_groups = [
            self.included_symbols,
            self.crypto_symbols,
            self.manual_symbols,
            self.watchlist_symbols,
            self.major_crypto_symbols,
        ]
        for group in candidate_groups:
            filtered = _filter_crypto_symbols(group)
            if filtered:
                return filtered
        return []

    @property
    def active_symbols(self) -> list[str]:
        if self.crypto_only_mode:
            return self.active_crypto_symbols
        # Support INCLUDED_SYMBOLS as backward-compatible alias for DEFAULT_SYMBOLS
        if self.included_symbols:
            return _dedupe_symbols(self.included_symbols)
        return self.manual_symbols

    @property
    def scan_symbol_allowlist(self) -> list[str]:
        if self.crypto_only_mode:
            return self.active_crypto_symbols
        if self.included_symbols:
            return _dedupe_symbols(self.included_symbols)
        return []

    @property
    def active_asset_classes(self) -> list[str]:
        return sorted(item.value for item in self.enabled_asset_class_set)

    @property
    def primary_runtime_asset_class(self) -> AssetClass:
        if self.crypto_only_mode:
            return AssetClass.CRYPTO
        for candidate in (AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO, AssetClass.OPTION):
            if candidate in self.enabled_asset_class_set:
                return candidate
        return AssetClass.EQUITY

    @property
    def primary_runtime_strategy(self) -> str:
        return self.strategy_for_asset_class(self.primary_runtime_asset_class)

    @property
    def resolved_trading_profile(self) -> ResolvedTradingProfile:
        return resolve_trading_profile(self)

    @property
    def trading_profile_summary(self) -> dict[str, object]:
        return self.resolved_trading_profile.to_dict()

    @property
    def effective_trading_profile(self) -> str:
        return self.resolved_trading_profile.name

    @property
    def effective_scan_interval_seconds(self) -> int:
        intervals = list(self.resolved_trading_profile.scan_interval_seconds_by_asset_class.values())
        if not intervals:
            return max(1, int(self.scan_interval_seconds))
        return max(1, min(intervals))

    @property
    def effective_max_positions_total(self) -> int:
        return int(self.resolved_trading_profile.max_positions_total)

    @property
    def effective_risk_per_trade_pct(self) -> float:
        return float(self.resolved_trading_profile.risk_per_trade_pct)

    @property
    def effective_max_symbol_allocation_pct(self) -> float:
        return float(self.resolved_trading_profile.max_symbol_allocation_pct)

    @property
    def effective_allow_extended_hours(self) -> bool:
        return bool(self.resolved_trading_profile.allow_extended_hours)

    @property
    def effective_short_selling_enabled(self) -> bool:
        return bool(self.resolved_trading_profile.short_selling_enabled)

    @property
    def effective_scale_in_mode(self) -> str:
        return str(self.resolved_trading_profile.scale_in_mode)

    @property
    def effective_min_bars_between_tranches(self) -> int:
        return int(self.resolved_trading_profile.min_bars_between_tranches)

    @property
    def effective_minutes_between_tranches(self) -> int:
        return int(self.resolved_trading_profile.minutes_between_tranches)

    @property
    def effective_add_on_favorable_move_pct(self) -> float:
        return float(self.resolved_trading_profile.add_on_favorable_move_pct)

    @property
    def effective_ml_min_score_threshold(self) -> float:
        return float(self.resolved_trading_profile.ml_min_score_threshold)

    @property
    def effective_news_catalyst_weight(self) -> float:
        return float(self.resolved_trading_profile.news_catalyst_weight)

    @property
    def enabled_news_sources(self) -> list[str]:
        active_sources = ["reuters", "marketwatch"]
        if self.benzinga_rss_enabled:
            active_sources.append("benzinga")
        if self.sec_rss_enabled:
            active_sources.append("sec")
        if self.news_source_ids:
            active_set = set(self.news_source_ids)
            if "default_rss" in active_set:
                active_set.update({"reuters", "marketwatch"})
            return [source for source in active_sources if source in active_set]
        return active_sources

    @field_validator("default_symbols", "crypto_symbols", "major_equity_symbols", "major_crypto_symbols", mode="before")
    def parse_default_symbols(cls, value: str | List[str]) -> List[str]:
        return _parse_json_list(value, "SYMBOLS")

    @field_validator("enabled_asset_classes", mode="before")
    def parse_enabled_asset_classes(cls, value: str | list[str]) -> list[str]:
        return [item.lower() for item in _parse_json_list(value, "ENABLED_ASSET_CLASSES")]

    @field_validator("news_source_ids", mode="before")
    def parse_news_source_ids(cls, value: str | list[str]) -> list[str]:
        return [item.lower() for item in _parse_string_list(value, "NEWS_SOURCE_IDS")]

    @field_validator("benzinga_rss_urls", "sec_rss_urls", mode="before")
    def parse_url_lists(cls, value: str | list[str], info: ValidationInfo) -> list[str]:
        return _parse_string_list(value, info.field_name.upper())

    @field_validator("entry_tranche_weights", mode="before")
    def parse_entry_tranche_weights(cls, value: str | list[float] | list[str]) -> list[float]:
        return _parse_numeric_list(value, "ENTRY_TRANCHE_WEIGHTS")

    @field_validator("partial_take_profit_levels", "partial_take_profit_fractions", mode="before")
    def parse_partial_exit_lists(cls, value: str | list[float] | list[str], info: ValidationInfo) -> list[float]:
        return _parse_numeric_list(value, info.field_name.upper())

    @field_validator("discord_webhook_url", mode="before")
    def parse_discord_webhook_url(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "alpaca_api_key",
        "alpaca_secret_key",
        "api_admin_token",
        "openai_api_key",
        "coingecko_api_key",
        "tradier_api_token",
        mode="before",
    )
    def parse_optional_secret_value(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("excluded_symbols", "included_symbols", mode="before")
    def parse_symbol_lists(cls, value: str | list[str], info: ValidationInfo) -> list[str]:
        return _parse_json_list(value, info.field_name.upper())

    @field_validator("market_data_fallback_providers", mode="before")
    def parse_market_data_fallback_providers(cls, value: str | list[str]) -> list[str]:
        return [item.strip().lower() for item in _parse_string_list(value, "MARKET_DATA_FALLBACK_PROVIDERS") if item.strip()]

    @field_validator("watchlists", mode="before")
    def parse_watchlists(cls, value: str | dict[str, Any]) -> dict[str, list[str]]:
        parsed = _parse_json_object(value, "WATCHLISTS")
        return {
            str(name): [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
            for name, symbols in parsed.items()
            if isinstance(symbols, list)
        }

    @field_validator(
        "max_positions_per_asset_class",
        "scan_interval_seconds_by_asset_class",
        "entry_timeframe_by_asset_class",
        "regime_timeframe_by_asset_class",
        "scanner_timeframe_by_asset_class",
        "lookback_bars_by_asset_class",
        "universe_prefilter_limit_by_asset_class",
        "final_evaluation_limit_by_asset_class",
        "max_notional_per_asset_class",
        "max_asset_class_allocation_pct",
        "dust_position_max_qty_by_asset_class",
        "strategy_switches",
        "active_strategy_by_asset_class",
        "provider_rate_limits_per_minute",
        "news_source_weights",
        "aggressive_max_positions_per_asset_class",
        "aggressive_scan_interval_seconds_by_asset_class",
        "aggressive_universe_prefilter_limit_by_asset_class",
        "aggressive_final_evaluation_limit_by_asset_class",
        mode="before",
    )
    def parse_json_objects(cls, value: str | dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        return _parse_json_object(value, info.field_name.upper())

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        mode = self.broker_mode.lower()
        if mode == "alpaca":
            mode = "paper"
        supported_modes = {"paper", "mock"}
        if mode not in supported_modes:
            raise ValueError(
                "BROKER_MODE must be one of ['mock', 'paper']; "
                f"got '{self.broker_mode}'. The legacy alias 'alpaca' is still accepted."
            )
        self.broker_mode = mode
        self.trading_profile = str(self.trading_profile or "conservative").strip().lower()
        if self.aggressive_mode_enabled and self.trading_profile == "conservative":
            self.trading_profile = "aggressive"
        if self.trading_profile not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("TRADING_PROFILE must be one of: conservative, balanced, aggressive.")
        self.aggressive_profile_version = str(self.aggressive_profile_version or "v1").strip() or "v1"

        if self.strategy_name:
            self.active_strategy = self.strategy_name
        self.active_strategy = {
            "regime_momentum_breakout": "equity_momentum_breakout",
        }.get(self.active_strategy.strip().lower(), self.active_strategy.strip().lower())
        if not self.active_strategy:
            raise ValueError("ACTIVE_STRATEGY must not be empty.")
        self.strategy_name = self.active_strategy
        self.active_strategy_by_asset_class = {
            str(key).strip().lower(): str(value).strip().lower()
            for key, value in self.active_strategy_by_asset_class.items()
            if str(key).strip() and str(value).strip()
        }
        self.news_source_ids = list(dict.fromkeys(item.lower() for item in self.news_source_ids if str(item).strip()))
        self.benzinga_rss_urls = [item for item in self.benzinga_rss_urls if str(item).strip()]
        self.sec_rss_urls = [item for item in self.sec_rss_urls if str(item).strip()]
        self.news_source_weights = {
            str(key).strip().lower(): float(value)
            for key, value in self.news_source_weights.items()
            if str(key).strip()
        }
        allowed_market_data_providers = {"composite", "alpaca", "yfinance", "coingecko", "tradier", "csv", "mock"}
        self.market_data_provider_default = str(self.market_data_provider_default or "composite").strip().lower()
        if self.market_data_provider_default not in allowed_market_data_providers:
            raise ValueError(
                "MARKET_DATA_PROVIDER_DEFAULT must be one of: alpaca, coingecko, composite, csv, mock, tradier, yfinance."
            )
        for field_name in ("equity_data_provider", "etf_data_provider", "crypto_data_provider", "option_data_provider"):
            provider_name = str(getattr(self, field_name) or "").strip().lower()
            if not provider_name:
                provider_name = "composite"
            if provider_name not in allowed_market_data_providers:
                raise ValueError(f"{field_name.upper()} must be a supported market data provider.")
            setattr(self, field_name, provider_name)
        self.market_data_fallback_providers = [
            provider
            for provider in dict.fromkeys(str(item).strip().lower() for item in self.market_data_fallback_providers)
            if provider and provider in allowed_market_data_providers and provider != "composite"
        ]
        self.provider_rate_limits_per_minute = {
            str(key).strip().lower(): max(0, int(value))
            for key, value in self.provider_rate_limits_per_minute.items()
            if str(key).strip()
        }
        for provider_name, default_limit in {"alpaca": 120, "yfinance": 30, "coingecko": 25, "tradier": 60}.items():
            self.provider_rate_limits_per_minute.setdefault(provider_name, default_limit)

        if self.position_notional_buffer_pct <= 0 or self.position_notional_buffer_pct > 1:
            raise ValueError("POSITION_NOTIONAL_BUFFER_PCT must be greater than 0 and less than or equal to 1.")
        if not 0 < self.discord_dedupe_ttl_seconds:
            raise ValueError("DISCORD_DEDUPE_TTL_SECONDS must be greater than 0.")
        if self.broker_order_status_ignore_terminal_older_than_minutes < 0:
            raise ValueError("BROKER_ORDER_STATUS_IGNORE_TERMINAL_OLDER_THAN_MINUTES must be >= 0.")
        if self.quote_stale_after_seconds < 0:
            raise ValueError("QUOTE_STALE_AFTER_SECONDS must be >= 0.")
        if self.snapshot_cache_ttl_seconds < 0:
            raise ValueError("SNAPSHOT_CACHE_TTL_SECONDS must be >= 0.")
        if self.intraday_bars_cache_ttl_seconds < 0:
            raise ValueError("INTRADAY_BARS_CACHE_TTL_SECONDS must be >= 0.")
        if self.daily_bars_cache_ttl_seconds < 0:
            raise ValueError("DAILY_BARS_CACHE_TTL_SECONDS must be >= 0.")
        if self.option_chain_cache_ttl_seconds < 0:
            raise ValueError("OPTION_CHAIN_CACHE_TTL_SECONDS must be >= 0.")
        if self.market_data_max_retries < 0:
            raise ValueError("MARKET_DATA_MAX_RETRIES must be >= 0.")
        if self.market_data_backoff_base_seconds < 0:
            raise ValueError("MARKET_DATA_BACKOFF_BASE_SECONDS must be >= 0.")
        if self.market_data_backoff_max_seconds < 0:
            raise ValueError("MARKET_DATA_BACKOFF_MAX_SECONDS must be >= 0.")
        if self.entry_tranches <= 0:
            raise ValueError("ENTRY_TRANCHES must be greater than 0.")
        if len(self.entry_tranche_weights) != self.entry_tranches:
            raise ValueError(
                "ENTRY_TRANCHES must match the number of ENTRY_TRANCHE_WEIGHTS values."
            )
        if any(weight <= 0 for weight in self.entry_tranche_weights):
            raise ValueError("ENTRY_TRANCHE_WEIGHTS values must all be greater than 0.")
        total_weight = float(sum(self.entry_tranche_weights))
        if abs(total_weight - 1.0) > 1e-6:
            raise ValueError(
                f"ENTRY_TRANCHE_WEIGHTS must sum to 1.0 (got {total_weight:.6f})."
            )
        if self.max_symbol_allocation_pct <= 0 or self.max_symbol_allocation_pct > 1:
            raise ValueError("MAX_SYMBOL_ALLOCATION_PCT must be between 0 and 1.")
        if self.aggressive_max_symbol_allocation_pct <= 0 or self.aggressive_max_symbol_allocation_pct > 1:
            raise ValueError("AGGRESSIVE_MAX_SYMBOL_ALLOCATION_PCT must be between 0 and 1.")
        if any(value <= 0 or value > 1 for value in self.max_asset_class_allocation_pct.values()):
            raise ValueError("MAX_ASSET_CLASS_ALLOCATION_PCT values must each be between 0 and 1.")
        self.scan_interval_seconds_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.scan_interval_seconds_by_asset_class.items()
            if str(key).strip()
        }
        self.aggressive_scan_interval_seconds_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.aggressive_scan_interval_seconds_by_asset_class.items()
            if str(key).strip()
        }
        self.entry_timeframe_by_asset_class = {
            str(key).strip().lower(): str(value).strip()
            for key, value in self.entry_timeframe_by_asset_class.items()
            if str(key).strip() and str(value).strip()
        }
        self.regime_timeframe_by_asset_class = {
            str(key).strip().lower(): str(value).strip()
            for key, value in self.regime_timeframe_by_asset_class.items()
            if str(key).strip() and str(value).strip()
        }
        self.scanner_timeframe_by_asset_class = {
            str(key).strip().lower(): str(value).strip()
            for key, value in self.scanner_timeframe_by_asset_class.items()
            if str(key).strip() and str(value).strip()
        }
        self.lookback_bars_by_asset_class = {
            str(key).strip().lower(): max(5, int(value))
            for key, value in self.lookback_bars_by_asset_class.items()
            if str(key).strip()
        }
        self.universe_prefilter_limit_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.universe_prefilter_limit_by_asset_class.items()
            if str(key).strip()
        }
        self.aggressive_universe_prefilter_limit_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.aggressive_universe_prefilter_limit_by_asset_class.items()
            if str(key).strip()
        }
        self.final_evaluation_limit_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.final_evaluation_limit_by_asset_class.items()
            if str(key).strip()
        }
        self.aggressive_final_evaluation_limit_by_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.aggressive_final_evaluation_limit_by_asset_class.items()
            if str(key).strip()
        }
        self.aggressive_max_positions_per_asset_class = {
            str(key).strip().lower(): max(1, int(value))
            for key, value in self.aggressive_max_positions_per_asset_class.items()
            if str(key).strip()
        }
        for asset_class, default_value in DEFAULT_ENTRY_TIMEFRAME_BY_ASSET_CLASS.items():
            self.entry_timeframe_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_REGIME_TIMEFRAME_BY_ASSET_CLASS.items():
            self.regime_timeframe_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_SCANNER_TIMEFRAME_BY_ASSET_CLASS.items():
            self.scanner_timeframe_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_LOOKBACK_BARS_BY_ASSET_CLASS.items():
            self.lookback_bars_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS.items():
            self.universe_prefilter_limit_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS.items():
            self.final_evaluation_limit_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_AGGRESSIVE_SCAN_INTERVAL_SECONDS_BY_ASSET_CLASS.items():
            self.aggressive_scan_interval_seconds_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_AGGRESSIVE_UNIVERSE_PREFILTER_LIMIT_BY_ASSET_CLASS.items():
            self.aggressive_universe_prefilter_limit_by_asset_class.setdefault(asset_class, default_value)
        for asset_class, default_value in DEFAULT_AGGRESSIVE_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS.items():
            self.aggressive_final_evaluation_limit_by_asset_class.setdefault(asset_class, default_value)
        for asset_class in DEFAULT_FINAL_EVALUATION_LIMIT_BY_ASSET_CLASS:
            final_limit = self.final_evaluation_limit_by_asset_class[asset_class]
            prefilter_limit = self.universe_prefilter_limit_by_asset_class.get(asset_class, final_limit)
            self.universe_prefilter_limit_by_asset_class[asset_class] = max(final_limit, prefilter_limit)
            aggressive_final_limit = self.aggressive_final_evaluation_limit_by_asset_class.get(asset_class, final_limit)
            aggressive_prefilter_limit = self.aggressive_universe_prefilter_limit_by_asset_class.get(
                asset_class,
                aggressive_final_limit,
            )
            self.aggressive_universe_prefilter_limit_by_asset_class[asset_class] = max(
                aggressive_final_limit,
                aggressive_prefilter_limit,
            )
        if self.dust_position_max_notional < 0:
            raise ValueError("DUST_POSITION_MAX_NOTIONAL must be >= 0.")
        self.dust_position_max_qty_by_asset_class = {
            str(key).strip().lower(): float(value)
            for key, value in self.dust_position_max_qty_by_asset_class.items()
            if str(key).strip()
        }
        if any(value < 0 for value in self.dust_position_max_qty_by_asset_class.values()):
            raise ValueError("DUST_POSITION_MAX_QTY_BY_ASSET_CLASS values must each be >= 0.")
        for asset_class in AssetClass:
            if asset_class == AssetClass.UNKNOWN:
                continue
            self.dust_position_max_qty_by_asset_class.setdefault(asset_class.value, 0.0)
        if self.symbol_reentry_cooldown_minutes < 0:
            raise ValueError("SYMBOL_REENTRY_COOLDOWN_MINUTES must be >= 0.")
        if self.max_consecutive_losing_exits < 1:
            raise ValueError("MAX_CONSECUTIVE_LOSING_EXITS must be >= 1.")
        if not self.partial_take_profit_levels:
            raise ValueError("PARTIAL_TAKE_PROFIT_LEVELS must not be empty.")
        if len(self.partial_take_profit_levels) != len(self.partial_take_profit_fractions):
            raise ValueError("PARTIAL_TAKE_PROFIT_LEVELS and PARTIAL_TAKE_PROFIT_FRACTIONS must have matching lengths.")
        if any(level <= 0 for level in self.partial_take_profit_levels):
            raise ValueError("PARTIAL_TAKE_PROFIT_LEVELS must contain only positive values.")
        if any(fraction <= 0 or fraction > 1 for fraction in self.partial_take_profit_fractions):
            raise ValueError("PARTIAL_TAKE_PROFIT_FRACTIONS values must be between 0 and 1.")
        if self.break_even_after_r_multiple < 0:
            raise ValueError("BREAK_EVEN_AFTER_R_MULTIPLE must be >= 0.")
        self.trailing_stop_mode = self.trailing_stop_mode.strip().lower()
        if self.trailing_stop_mode not in {"none", "atr", "static"}:
            raise ValueError("TRAILING_STOP_MODE must be one of: none, atr, static.")
        if self.trailing_stop_atr_multiple < 0:
            raise ValueError("TRAILING_STOP_ATR_MULTIPLE must be >= 0.")
        if self.time_stop_bars < 0:
            raise ValueError("TIME_STOP_BARS must be >= 0.")
        self.scale_in_mode = self.scale_in_mode.strip().lower()
        if self.scale_in_mode not in {"confirmation", "time", "momentum"}:
            raise ValueError("SCALE_IN_MODE must be one of: confirmation, time, momentum.")
        self.log_level = self.log_level.strip().upper()
        self.ml_model_type = self.ml_model_type.strip().lower()
        if self.ml_model_type not in {"logistic_regression", "xgboost"}:
            raise ValueError("ML_MODEL_TYPE must be one of: logistic_regression, xgboost.")
        if not 0 <= self.ml_min_score_threshold <= 1:
            raise ValueError("ML_MIN_SCORE_THRESHOLD must be between 0 and 1.")
        if not 0 <= self.ml_exit_min_score <= 1:
            raise ValueError("ML_EXIT_MIN_SCORE must be between 0 and 1.")
        if self.ml_min_train_rows < 1:
            raise ValueError("ML_MIN_TRAIN_ROWS must be >= 1.")
        if not 0 <= self.ml_entry_min_auc <= 1:
            raise ValueError("ML_ENTRY_MIN_AUC must be between 0 and 1.")
        if not 0 <= self.ml_entry_min_precision <= 1:
            raise ValueError("ML_ENTRY_MIN_PRECISION must be between 0 and 1.")
        if not 0 <= self.ml_promotion_min_auc <= 1:
            raise ValueError("ML_PROMOTION_MIN_AUC must be between 0 and 1.")
        if not 0 <= self.ml_promotion_min_precision <= 1:
            raise ValueError("ML_PROMOTION_MIN_PRECISION must be between 0 and 1.")
        if self.ml_promotion_min_profit_factor < 0:
            raise ValueError("ML_PROMOTION_MIN_PROFIT_FACTOR must be >= 0.")
        if not 0 <= self.ml_promotion_max_drawdown <= 1:
            raise ValueError("ML_PROMOTION_MAX_DRAWDOWN must be between 0 and 1.")
        if self.news_max_headlines_per_ticker < 1:
            raise ValueError("NEWS_MAX_HEADLINES_PER_TICKER must be >= 1.")
        if self.news_lookback_hours < 1:
            raise ValueError("NEWS_LOOKBACK_HOURS must be >= 1.")
        if self.news_fetch_timeout_seconds <= 0:
            raise ValueError("NEWS_FETCH_TIMEOUT_SECONDS must be > 0.")
        if self.news_fetch_retry_count < 0:
            raise ValueError("NEWS_FETCH_RETRY_COUNT must be >= 0.")
        if self.news_fetch_backoff_seconds < 0:
            raise ValueError("NEWS_FETCH_BACKOFF_SECONDS must be >= 0.")
        if self.news_dedupe_window_minutes < 0:
            raise ValueError("NEWS_DEDUPE_WINDOW_MINUTES must be >= 0.")
        if self.min_bars_between_tranches < 0:
            raise ValueError("MIN_BARS_BETWEEN_TRANCHES must be >= 0.")
        if self.minutes_between_tranches < 0:
            raise ValueError("MINUTES_BETWEEN_TRANCHES must be >= 0.")
        if self.add_on_favorable_move_pct < 0:
            raise ValueError("ADD_ON_FAVORABLE_MOVE_PCT must be >= 0.")
        if self.aggressive_max_positions < 1:
            raise ValueError("AGGRESSIVE_MAX_POSITIONS must be >= 1.")
        if not 0 < self.aggressive_risk_per_trade_pct <= 1:
            raise ValueError("AGGRESSIVE_RISK_PER_TRADE_PCT must be between 0 and 1.")
        if not -1 <= self.aggressive_entry_threshold_adjustment <= 1:
            raise ValueError("AGGRESSIVE_ENTRY_THRESHOLD_ADJUSTMENT must be between -1 and 1.")
        if self.aggressive_news_catalyst_weight < 0:
            raise ValueError("AGGRESSIVE_NEWS_CATALYST_WEIGHT must be >= 0.")
        if not self.rate_limit_default.strip():
            raise ValueError("RATE_LIMIT_DEFAULT must not be empty.")
        if not self.rate_limit_scanner.strip():
            raise ValueError("RATE_LIMIT_SCANNER must not be empty.")
        if not self.rate_limit_admin.strip():
            raise ValueError("RATE_LIMIT_ADMIN must not be empty.")
        if not self.rate_limit_market.strip():
            raise ValueError("RATE_LIMIT_MARKET must not be empty.")
        if not self.rate_limit_signals.strip():
            raise ValueError("RATE_LIMIT_SIGNALS must not be empty.")

        if self.is_paper_mode and not self.has_alpaca_credentials:
            raise ValueError(
                "BROKER_MODE=paper requires ALPACA_API_KEY and ALPACA_SECRET_KEY to be set."
            )
        if not self.live_trading_enabled and self.is_alpaca_mode and "paper-api" not in str(self.alpaca_base_url):
            raise ValueError(
                "ALPACA_BASE_URL must point to Alpaca paper trading unless LIVE_TRADING_ENABLED=true."
            )
        if self.live_trading_enabled:
            if not self.is_alpaca_mode:
                raise ValueError("LIVE_TRADING_ENABLED is only supported with BROKER_MODE=paper.")
            if self.live_trading_ack != "ENABLE_LIVE_TRADING":
                raise ValueError(
                    "Set LIVE_TRADING_ACK=ENABLE_LIVE_TRADING to explicitly acknowledge live trading risk."
                )
        if self.discord_notifications_enabled and not self.discord_webhook_url:
            raise ValueError(
                "DISCORD_NOTIFICATIONS_ENABLED=true requires DISCORD_WEBHOOK_URL to be set."
            )
        if is_placeholder_discord_webhook_url(str(self.discord_webhook_url) if self.discord_webhook_url else None):
            raise ValueError(
                "DISCORD_WEBHOOK_URL must be a real Discord webhook URL, not a placeholder such as "
                "'your_webhook_id/your_webhook_token'."
            )
        if self.max_notional_per_position != self.max_position_notional:
            resolved_notional_cap = min(self.max_notional_per_position, self.max_position_notional)
            self.max_notional_per_position = resolved_notional_cap
            self.max_position_notional = resolved_notional_cap
        if self.max_positions_total != self.max_positions:
            self.max_positions = self.max_positions_total
        if self.max_concurrent_positions != self.max_positions_total:
            resolved_positions_cap = min(self.max_concurrent_positions, self.max_positions_total)
            self.max_concurrent_positions = resolved_positions_cap
            self.max_positions_total = resolved_positions_cap
            self.max_positions = resolved_positions_cap
        if self.risk_per_trade_pct != self.max_risk_per_trade:
            if self.risk_per_trade_pct != 0.01:
                self.max_risk_per_trade = self.risk_per_trade_pct
            else:
                self.risk_per_trade_pct = self.max_risk_per_trade
        if self.ml_entry_current_model_path != "models/current_model.joblib":
            self.ml_current_model_path = self.ml_entry_current_model_path
        elif self.ml_current_model_path != "models/current_model.joblib":
            self.ml_entry_current_model_path = self.ml_current_model_path
        if self.ml_entry_candidate_model_path != "models/candidate_model.joblib":
            self.ml_candidate_model_path = self.ml_entry_candidate_model_path
        elif self.ml_candidate_model_path != "models/candidate_model.joblib":
            self.ml_entry_candidate_model_path = self.ml_candidate_model_path
        return self

    def strategy_for_asset_class(self, asset_class: AssetClass | str) -> str:
        key = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()
        mapped = self.active_strategy_by_asset_class.get(key)
        if mapped:
            return mapped
        if key == AssetClass.CRYPTO.value and self.active_strategy != "crypto_momentum_trend":
            return "crypto_momentum_trend"
        return self.active_strategy

    def candidate_strategies_for_asset_class(self, asset_class: AssetClass | str) -> list[str]:
        key = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()
        resolved = self.resolved_trading_profile.strategies_for_asset_class(key)
        if resolved:
            return resolved
        return [self.strategy_for_asset_class(key)]

    def _asset_class_key(self, asset_class: AssetClass | str) -> str:
        return asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()

    def entry_timeframe_for_asset_class(self, asset_class: AssetClass | str) -> str:
        key = self._asset_class_key(asset_class)
        return str(self.entry_timeframe_by_asset_class.get(key) or self.default_timeframe).strip()

    def regime_timeframe_for_asset_class(self, asset_class: AssetClass | str) -> str:
        key = self._asset_class_key(asset_class)
        return str(self.regime_timeframe_by_asset_class.get(key) or self.default_timeframe).strip()

    def scanner_timeframe_for_asset_class(self, asset_class: AssetClass | str) -> str:
        key = self._asset_class_key(asset_class)
        return str(self.scanner_timeframe_by_asset_class.get(key) or self.default_timeframe).strip()

    def lookback_bars_for_asset_class(self, asset_class: AssetClass | str) -> int:
        key = self._asset_class_key(asset_class)
        value = self.lookback_bars_by_asset_class.get(key)
        if value is None:
            return max(30, self.scanner_limit_per_asset_class)
        return max(5, int(value))

    def scan_interval_for_asset_class(self, asset_class: AssetClass | str) -> int:
        key = self._asset_class_key(asset_class)
        value = self.resolved_trading_profile.scan_interval_seconds_by_asset_class.get(key)
        if value is None:
            return max(1, int(self.scan_interval_seconds))
        return max(1, int(value))

    def universe_prefilter_limit_for_asset_class(self, asset_class: AssetClass | str) -> int:
        key = self._asset_class_key(asset_class)
        final_limit = self.final_evaluation_limit_for_asset_class(asset_class)
        value = self.resolved_trading_profile.universe_prefilter_limit_by_asset_class.get(key)
        if value is None:
            return max(final_limit, self.scanner_limit_per_asset_class)
        return max(final_limit, int(value))

    def final_evaluation_limit_for_asset_class(self, asset_class: AssetClass | str) -> int:
        key = self._asset_class_key(asset_class)
        value = self.resolved_trading_profile.final_evaluation_limit_by_asset_class.get(key)
        if value is None:
            return max(1, min(self.scanner_limit_per_asset_class, 15))
        return max(1, int(value))

    def max_positions_for_asset_class(self, asset_class: AssetClass | str) -> int:
        key = self._asset_class_key(asset_class)
        value = self.resolved_trading_profile.max_positions_per_asset_class.get(key)
        if value is None:
            return self.effective_max_positions_total
        return max(1, int(value))

    @property
    def news_llm_status(self) -> str:
        if not self.news_features_enabled:
            return "news_features_disabled"
        if not self.news_rss_enabled:
            return "news_rss_disabled"
        if not self.news_llm_enabled:
            return "news_llm_disabled"
        if not self.openai_api_key:
            return "openai_api_key_missing"
        return "available"

    @property
    def news_llm_available(self) -> bool:
        return self.news_llm_status == "available"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
