from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import httpx

from app.config.settings import Settings, get_settings


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float
    price: float | None = None
    is_dry_run: bool = True


@dataclass
class BrokerAccount:
    cash: float
    equity: float
    positions: int
    buying_power: float
    mode: str
    trading_enabled: bool


class BrokerInterface:
    """Abstract interface for brokers."""

    def get_account(self) -> BrokerAccount:
        raise NotImplementedError

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        raise NotImplementedError

    def close_position(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def list_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_latest_price(self, symbol: str) -> float:
        raise NotImplementedError


class PaperBroker(BrokerInterface):
    """Simple in-memory broker for paper trading and testing."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.cash = 100_000.0
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []

    def get_account(self) -> BrokerAccount:
        equity = self.calculate_equity()
        return BrokerAccount(
            cash=self.cash,
            equity=equity,
            positions=len(self.positions),
            buying_power=self.cash,
            mode=self.settings.broker_mode,
            trading_enabled=self.settings.trading_enabled,
        )

    def get_positions(self) -> list[dict[str, Any]]:
        return [pos.copy() for pos in self.positions.values()]

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        if not self.settings.is_paper_mode:
            raise RuntimeError("PaperBroker only supports paper mode.")

        price = order.price or self.get_latest_price(order.symbol)
        filled_qty = order.quantity
        self.orders.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": filled_qty,
                "price": price,
                "status": "FILLED" if not order.is_dry_run else "DRY_RUN",
                "executed_at": datetime.datetime.utcnow().isoformat(),
                "is_dry_run": order.is_dry_run,
            }
        )

        if not order.is_dry_run:
            self.apply_trade(order.symbol, order.side, filled_qty, price)

        return self.orders[-1]

    def apply_trade(self, symbol: str, side: str, quantity: float, price: float) -> None:
        if side.upper() == "BUY":
            self.cash -= quantity * price
            self.positions[symbol] = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": price,
                "side": side,
                "current_price": price,
            }
        else:
            if symbol in self.positions:
                self.cash += quantity * price
                self.positions.pop(symbol, None)

    def close_position(self, symbol: str) -> dict[str, Any]:
        if symbol not in self.positions:
            return {"message": "No position to close", "symbol": symbol}

        position = self.positions[symbol]
        price = self.get_latest_price(symbol)
        self.cash += position["quantity"] * price
        self.positions.pop(symbol, None)
        return {"symbol": symbol, "closed_at": datetime.datetime.utcnow().isoformat(), "price": price}

    def list_orders(self) -> list[dict[str, Any]]:
        return [order.copy() for order in self.orders]

    def get_latest_price(self, symbol: str) -> float:
        base_prices = {"AAPL": 170.0, "SPY": 470.0, "QQQ": 380.0}
        return base_prices.get(symbol.upper(), 100.0)

    def calculate_equity(self) -> float:
        total = self.cash
        for position in self.positions.values():
            total += position["quantity"] * position["current_price"]
        return total


class AlpacaBroker(BrokerInterface):
    """Adapter for Alpaca paper trading API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.is_alpaca_mode:
            raise RuntimeError("AlpacaBroker is only available when BROKER_MODE=alpaca.")
        if not self.settings.has_alpaca_credentials:
            raise ValueError(
                "Alpaca paper trading requires ALPACA_API_KEY and ALPACA_SECRET_KEY."
            )

        self.base_url = str(self.settings.alpaca_base_url).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            timeout=10.0,
        )

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.request(method, path, params=params, json=json)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Alpaca API returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Alpaca request failed: {exc}") from exc

    def get_account(self) -> BrokerAccount:
        data = self._request("GET", "/v2/account")
        return BrokerAccount(
            cash=float(data.get("cash", 0.0)),
            equity=float(data.get("equity", 0.0)),
            positions=len(data.get("positions", [])),
            buying_power=float(data.get("buying_power", 0.0)),
            mode=self.settings.broker_mode,
            trading_enabled=self.settings.trading_enabled,
        )

    def get_positions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/positions")
        if not isinstance(response, list):
            return []
        return response

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        if order.is_dry_run:
            return {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "price": order.price,
                "status": "DRY_RUN",
                "is_dry_run": True,
            }

        payload = {
            "symbol": order.symbol,
            "qty": order.quantity,
            "side": order.side.lower(),
            "type": "market",
            "time_in_force": "day",
        }
        return self._request("POST", "/v2/orders", json=payload)

    def close_position(self, symbol: str) -> dict[str, Any]:
        if not self.settings.trading_enabled:
            return {"symbol": symbol, "status": "DRY_RUN", "is_dry_run": True}
        return self._request("DELETE", f"/v2/positions/{symbol}")

    def list_orders(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/orders", params={"status": "all", "limit": 50})
        if not isinstance(response, list):
            return []
        return response

    def get_latest_price(self, symbol: str) -> float:
        path = f"/v2/stocks/{symbol}/bars"
        response = self._request(
            "GET",
            path,
            params={"timeframe": self.settings.default_timeframe, "limit": 1},
        )
        bars = response.get("bars") or []
        if not bars:
            raise RuntimeError(f"No bar data available for {symbol}")
        return float(bars[-1].get("c", bars[-1].get("close", 0.0)))


def create_broker(settings: Settings | None = None) -> BrokerInterface:
    settings = settings or get_settings()
    mode = settings.broker_mode.lower()
    if mode in {"paper", "mock"}:
        return PaperBroker(settings)
    if mode == "alpaca":
        return AlpacaBroker(settings)
    raise ValueError(f"Unsupported broker mode '{settings.broker_mode}'.")
