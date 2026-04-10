from __future__ import annotations

import json
from typing import Any, List

from pydantic import AnyHttpUrl, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings

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


class Settings(BaseSettings):
    app_env: str = Field("development", env="APP_ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    database_url: str = Field("sqlite:///./trading.db", env="DATABASE_URL")
    broker_mode: str = Field("paper", env="BROKER_MODE")
    alpaca_api_key: str | None = Field(None, env="ALPACA_API_KEY")
    alpaca_secret_key: str | None = Field(None, env="ALPACA_SECRET_KEY")
    alpaca_base_url: AnyHttpUrl = Field("https://paper-api.alpaca.markets", env="ALPACA_BASE_URL")
    alpaca_crypto_location: str = Field("us", env="ALPACA_CRYPTO_LOCATION")
    trading_enabled: bool = Field(False, env="TRADING_ENABLED")
    live_trading_enabled: bool = Field(False, env="LIVE_TRADING_ENABLED")
    live_trading_ack: str | None = Field(None, env="LIVE_TRADING_ACK")
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
    scanner_limit_per_asset_class: int = Field(50, env="SCANNER_LIMIT_PER_ASSET_CLASS")
    strategy_switches: dict[str, bool] = Field(default_factory=dict, env="STRATEGY_SWITCHES")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def is_paper_mode(self) -> bool:
        return self.broker_mode.lower() in {"paper", "mock"}

    @property
    def is_alpaca_mode(self) -> bool:
        return self.broker_mode.lower() == "alpaca"

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def is_live_enabled(self) -> bool:
        return self.trading_enabled and self.is_alpaca_mode and self.live_trading_enabled

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

    @field_validator("default_symbols", mode="before")
    def parse_default_symbols(cls, value: str | List[str]) -> List[str]:
        return _parse_json_list(value, "DEFAULT_SYMBOLS")

    @field_validator("enabled_asset_classes", mode="before")
    def parse_enabled_asset_classes(cls, value: str | list[str]) -> list[str]:
        return [item.lower() for item in _parse_json_list(value, "ENABLED_ASSET_CLASSES")]

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
        mode="before",
    )
    def parse_json_objects(cls, value: str | dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        return _parse_json_object(value, info.field_name.upper())

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        mode = self.broker_mode.lower()
        supported_modes = {"paper", "mock", "alpaca"}
        if mode not in supported_modes:
            raise ValueError(
                f"BROKER_MODE must be one of {sorted(supported_modes)}; got '{self.broker_mode}'."
            )

        if mode == "alpaca" and not self.has_alpaca_credentials:
            raise ValueError(
                "BROKER_MODE=alpaca requires ALPACA_API_KEY and ALPACA_SECRET_KEY to be set."
            )
        if not self.live_trading_enabled and self.is_alpaca_mode and "paper-api" not in str(self.alpaca_base_url):
            raise ValueError(
                "ALPACA_BASE_URL must point to Alpaca paper trading unless LIVE_TRADING_ENABLED=true."
            )
        if self.live_trading_enabled:
            if not self.is_alpaca_mode:
                raise ValueError("LIVE_TRADING_ENABLED is only supported with BROKER_MODE=alpaca.")
            if self.live_trading_ack != "ENABLE_LIVE_TRADING":
                raise ValueError(
                    "Set LIVE_TRADING_ACK=ENABLE_LIVE_TRADING to explicitly acknowledge live trading risk."
                )
        if self.max_notional_per_position != self.max_position_notional:
            self.max_position_notional = self.max_notional_per_position
        if self.max_positions_total != self.max_positions:
            self.max_positions = self.max_positions_total
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
