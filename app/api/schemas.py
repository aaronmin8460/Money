from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BacktestRequest(BaseModel):
    symbol: str | None = None
    csv_path: str | None = None


class RunOnceRequest(BaseModel):
    symbol: str | None = None


class BrokerStatus(BaseModel):
    broker_mode: str
    trading_enabled: bool
    has_credentials: bool
    safe_dry_run: bool
    broker_label: str


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
