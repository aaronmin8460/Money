from __future__ import annotations

import json
from typing import List

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = Field("development", env="APP_ENV")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    database_url: str = Field("sqlite:///./trading.db", env="DATABASE_URL")
    broker_mode: str = Field("paper", env="BROKER_MODE")
    alpaca_api_key: str | None = Field(None, env="ALPACA_API_KEY")
    alpaca_secret_key: str | None = Field(None, env="ALPACA_SECRET_KEY")
    alpaca_base_url: AnyHttpUrl = Field("https://paper-api.alpaca.markets", env="ALPACA_BASE_URL")
    trading_enabled: bool = Field(False, env="TRADING_ENABLED")
    max_risk_per_trade: float = Field(0.01, env="MAX_RISK_PER_TRADE")
    max_daily_loss_pct: float = Field(0.02, env="MAX_DAILY_LOSS_PCT")
    max_drawdown_pct: float = Field(0.10, env="MAX_DRAWDOWN_PCT")
    max_positions: int = Field(3, env="MAX_POSITIONS")
    default_timeframe: str = Field("1D", env="DEFAULT_TIMEFRAME")
    default_symbols: List[str] = Field(default_factory=lambda: ["AAPL", "SPY"], env="DEFAULT_SYMBOLS")

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
        return self.trading_enabled and self.is_alpaca_mode

    @validator("default_symbols", pre=True)
    def parse_default_symbols(cls, value: str | List[str]) -> List[str]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("DEFAULT_SYMBOLS must be a JSON array.")
                return [str(item).strip().upper() for item in parsed if item.strip()]
            except json.JSONDecodeError as e:
                raise ValueError(f"DEFAULT_SYMBOLS must be valid JSON: {e}")
        return value

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
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
