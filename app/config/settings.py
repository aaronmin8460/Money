from __future__ import annotations

import json
from typing import Annotated, Any, List

from pydantic import AnyHttpUrl, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from app.domain.models import AssetClass


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
    broker_mode: str = Field("paper", env="BROKER_MODE")
    alpaca_api_key: str | None = Field(None, env="ALPACA_API_KEY")
    alpaca_secret_key: str | None = Field(None, env="ALPACA_SECRET_KEY")
    alpaca_base_url: AnyHttpUrl = Field("https://paper-api.alpaca.markets", env="ALPACA_BASE_URL")
    alpaca_crypto_location: str = Field("us", env="ALPACA_CRYPTO_LOCATION")
    trading_enabled: bool = Field(False, env="TRADING_ENABLED")
    live_trading_enabled: bool = Field(False, env="LIVE_TRADING_ENABLED")
    live_trading_ack: str | None = Field(None, env="LIVE_TRADING_ACK")
    discord_notifications_enabled: bool = Field(False, env="DISCORD_NOTIFICATIONS_ENABLED")
    discord_webhook_url: AnyHttpUrl | None = Field(None, env="DISCORD_WEBHOOK_URL")
    discord_notify_dry_runs: bool = Field(False, env="DISCORD_NOTIFY_DRY_RUNS")
    discord_notify_rejections: bool = Field(True, env="DISCORD_NOTIFY_REJECTIONS")
    discord_notify_errors: bool = Field(True, env="DISCORD_NOTIFY_ERRORS")
    discord_notify_start_stop: bool = Field(True, env="DISCORD_NOTIFY_START_STOP")
    max_risk_per_trade: float = Field(0.01, env="MAX_RISK_PER_TRADE")
    max_daily_loss: float = Field(2_000.0, env="MAX_DAILY_LOSS")
    max_daily_loss_pct: float = Field(0.02, env="MAX_DAILY_LOSS_PCT")
    max_drawdown_pct: float = Field(0.10, env="MAX_DRAWDOWN_PCT")
    max_positions: int = Field(3, env="MAX_POSITIONS")
    max_positions_total: int = Field(3, env="MAX_POSITIONS_TOTAL")
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
    alpaca_data_base_url: AnyHttpUrl = Field("https://data.alpaca.markets", env="ALPACA_DATA_BASE_URL")
    max_position_notional: float = Field(10000.0, env="MAX_POSITION_NOTIONAL")
    position_notional_buffer_pct: float = Field(0.995, env="POSITION_NOTIONAL_BUFFER_PCT")
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
    max_correlated_positions: int = Field(2, env="MAX_CORRELATED_POSITIONS")
    cooldown_seconds_per_symbol: int = Field(300, env="COOLDOWN_SECONDS_PER_SYMBOL")
    cooldown_seconds_per_strategy: int = Field(180, env="COOLDOWN_SECONDS_PER_STRATEGY")
    take_profit_pct: float = Field(0.05, env="TAKE_PROFIT_PCT")
    stop_loss_atr_multiplier: float = Field(2.0, env="STOP_LOSS_ATR_MULTIPLIER")
    allow_extended_hours: bool = Field(False, env="ALLOW_EXTENDED_HOURS")
    kill_switch_enabled: bool = Field(False, env="KILL_SWITCH_ENABLED")
    short_selling_enabled: bool = Field(False, env="SHORT_SELLING_ENABLED")
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
    scan_universe_mode: str = Field("full", env="SCAN_UNIVERSE_MODE")
    major_equity_symbols: list[str] = Field(
        default_factory=lambda: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "IWM"],
        env="MAJOR_EQUITY_SYMBOLS",
    )
    major_crypto_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD"],
        env="MAJOR_CRYPTO_SYMBOLS",
    )
    prefer_primary_crypto_quotes: bool = Field(True, env="PREFER_PRIMARY_CRYPTO_QUOTES")
    ml_enabled: bool = Field(False, env="ML_ENABLED")
    ml_model_type: str = Field("logistic_regression", env="ML_MODEL_TYPE")
    ml_min_score_threshold: float = Field(0.55, env="ML_MIN_SCORE_THRESHOLD")
    ml_min_train_rows: int = Field(50, env="ML_MIN_TRAIN_ROWS")
    ml_retrain_enabled: bool = Field(False, env="ML_RETRAIN_ENABLED")
    ml_promotion_min_auc: float = Field(0.55, env="ML_PROMOTION_MIN_AUC")
    ml_promotion_min_precision: float = Field(0.50, env="ML_PROMOTION_MIN_PRECISION")
    ml_promotion_min_winrate_lift: float = Field(0.00, env="ML_PROMOTION_MIN_WINRATE_LIFT")
    model_dir: str = Field("models", env="MODEL_DIR")
    ml_current_model_path: str = Field("models/current_model.joblib", env="ML_CURRENT_MODEL_PATH")
    ml_candidate_model_path: str = Field("models/candidate_model.joblib", env="ML_CANDIDATE_MODEL_PATH")
    ml_registry_path: str = Field("models/registry.json", env="ML_REGISTRY_PATH")
    news_features_enabled: bool = Field(False, env="NEWS_FEATURES_ENABLED")
    news_rss_enabled: bool = Field(False, env="NEWS_RSS_ENABLED")
    news_llm_enabled: bool = Field(True, env="NEWS_LLM_ENABLED")
    openai_api_key: str | None = Field(None, env="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4.1-nano", env="OPENAI_MODEL")
    news_max_headlines_per_ticker: int = Field(8, env="NEWS_MAX_HEADLINES_PER_TICKER")
    news_lookback_hours: int = Field(24, env="NEWS_LOOKBACK_HOURS")

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
    def effective_max_position_notional(self) -> float:
        return float(self.max_position_notional * self.position_notional_buffer_pct)

    @property
    def enabled_asset_class_set(self) -> set[AssetClass]:
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
        return sorted({symbol.strip().upper() for symbol in values if symbol.strip()})

    @property
    def manual_symbols(self) -> list[str]:
        return self.default_symbols or self.watchlist_symbols

    @property
    def active_symbols(self) -> list[str]:
        # Support INCLUDED_SYMBOLS as backward-compatible alias for DEFAULT_SYMBOLS
        if self.included_symbols:
            return sorted({symbol.strip().upper() for symbol in self.included_symbols if symbol.strip()})
        return self.manual_symbols

    @field_validator("default_symbols", mode="before")
    def parse_default_symbols(cls, value: str | List[str]) -> List[str]:
        return _parse_json_list(value, "DEFAULT_SYMBOLS")

    @field_validator("enabled_asset_classes", mode="before")
    def parse_enabled_asset_classes(cls, value: str | list[str]) -> list[str]:
        return [item.lower() for item in _parse_json_list(value, "ENABLED_ASSET_CLASSES")]

    @field_validator("entry_tranche_weights", mode="before")
    def parse_entry_tranche_weights(cls, value: str | list[float] | list[str]) -> list[float]:
        return _parse_numeric_list(value, "ENTRY_TRANCHE_WEIGHTS")

    @field_validator("discord_webhook_url", mode="before")
    def parse_discord_webhook_url(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("openai_api_key", mode="before")
    def parse_openai_api_key(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("excluded_symbols", "included_symbols", mode="before")
    def parse_symbol_lists(cls, value: str | list[str], info: ValidationInfo) -> list[str]:
        return _parse_json_list(value, info.field_name.upper())

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
        "max_notional_per_asset_class",
        "strategy_switches",
        "active_strategy_by_asset_class",
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

        if self.position_notional_buffer_pct <= 0 or self.position_notional_buffer_pct > 1:
            raise ValueError("POSITION_NOTIONAL_BUFFER_PCT must be greater than 0 and less than or equal to 1.")
        if self.quote_stale_after_seconds < 0:
            raise ValueError("QUOTE_STALE_AFTER_SECONDS must be >= 0.")
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
        self.scale_in_mode = self.scale_in_mode.strip().lower()
        if self.scale_in_mode not in {"confirmation", "time", "momentum"}:
            raise ValueError("SCALE_IN_MODE must be one of: confirmation, time, momentum.")
        self.log_level = self.log_level.strip().upper()
        self.ml_model_type = self.ml_model_type.strip().lower()
        if self.ml_model_type not in {"logistic_regression", "xgboost"}:
            raise ValueError("ML_MODEL_TYPE must be one of: logistic_regression, xgboost.")
        if not 0 <= self.ml_min_score_threshold <= 1:
            raise ValueError("ML_MIN_SCORE_THRESHOLD must be between 0 and 1.")
        if self.ml_min_train_rows < 1:
            raise ValueError("ML_MIN_TRAIN_ROWS must be >= 1.")
        if not 0 <= self.ml_promotion_min_auc <= 1:
            raise ValueError("ML_PROMOTION_MIN_AUC must be between 0 and 1.")
        if not 0 <= self.ml_promotion_min_precision <= 1:
            raise ValueError("ML_PROMOTION_MIN_PRECISION must be between 0 and 1.")
        if self.news_max_headlines_per_ticker < 1:
            raise ValueError("NEWS_MAX_HEADLINES_PER_TICKER must be >= 1.")
        if self.news_lookback_hours < 1:
            raise ValueError("NEWS_LOOKBACK_HOURS must be >= 1.")
        if self.min_bars_between_tranches < 0:
            raise ValueError("MIN_BARS_BETWEEN_TRANCHES must be >= 0.")
        if self.minutes_between_tranches < 0:
            raise ValueError("MINUTES_BETWEEN_TRANCHES must be >= 0.")
        if self.add_on_favorable_move_pct < 0:
            raise ValueError("ADD_ON_FAVORABLE_MOVE_PCT must be >= 0.")

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
        return self

    def strategy_for_asset_class(self, asset_class: AssetClass | str) -> str:
        key = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class).strip().lower()
        mapped = self.active_strategy_by_asset_class.get(key)
        if mapped:
            return mapped
        if key == AssetClass.CRYPTO.value and self.active_strategy != "crypto_momentum_trend":
            return "crypto_momentum_trend"
        return self.active_strategy

    @property
    def news_llm_available(self) -> bool:
        return self.news_features_enabled and self.news_llm_enabled and bool(self.openai_api_key)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
