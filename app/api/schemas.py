from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    symbol: str | None = None
    csv_path: str | None = None


class RunOnceRequest(BaseModel):
    symbol: str | None = None
    asset_class: str | None = None


class SignalRunRequest(BaseModel):
    symbol: str | None = None
    asset_class: str | None = None
    limit: int = Field(10, ge=1, le=100)


class ResetLocalStateRequest(BaseModel):
    close_positions: bool = True
    cancel_open_orders: bool = True
    wipe_local_db: bool = False
    reset_daily_baseline_to_current_equity: bool = True


class BrokerStatus(BaseModel):
    broker_mode: str
    trading_enabled: bool
    has_credentials: bool
    safe_dry_run: bool
    broker_label: str
    live_trading_enabled: bool | None = None


class AccountSummary(BaseModel):
    cash: float
    equity: float
    positions: int
    buying_power: float
    mode: str
    trading_enabled: bool


class OrderResult(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    status: str
    is_dry_run: bool
    id: str | None = None
    client_order_id: str | None = None
    raw: Any | None = None


class RunOnceResult(BaseModel):
    symbol: str
    signal: str
    latest_price: float | None = None
    proposal: dict[str, Any]
    risk: dict[str, Any]
    action: str
    order: Any | None = None
    asset_class: str | None = None
    strategy_name: str | None = None
