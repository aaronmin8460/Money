from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config.settings import Settings, get_settings
from app.domain.models import AssetClass, AssetMetadata
from app.monitoring.logger import get_logger
from app.services.market_data import (
    CSVMarketDataService,
    MarketDataService,
    canonicalize_symbol,
    infer_asset_class,
    normalize_asset_class,
)

logger = get_logger("broker")


class BrokerError(Exception):
    """Base exception for broker-related errors."""


class BrokerAuthError(BrokerError):
    """Raised when authentication fails with the broker."""


class BrokerUpstreamError(BrokerError):
    """Raised when the broker API returns an error."""


class BrokerConnectionError(BrokerError):
    """Raised when there are network or connection issues."""


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float | None = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    notional: float | None = None
    price: float | None = None
    time_in_force: str | None = None
    order_type: str = "market"
    is_dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerAccount:
    cash: float
    equity: float
    positions: int
    buying_power: float
    mode: str
    trading_enabled: bool
    currency: str = "USD"


class BrokerInterface:
    """Abstract interface for brokers."""

    def get_account(self) -> BrokerAccount:
        raise NotImplementedError

    def list_assets(self, asset_class: AssetClass | str | None = None) -> list[AssetMetadata]:
        raise NotImplementedError

    def get_asset(self, symbol: str, asset_class: AssetClass | str | None = None) -> AssetMetadata | None:
        raise NotImplementedError

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        raise NotImplementedError

    def close_position(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def close_all_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def list_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def cancel_open_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        raise NotImplementedError

    def is_market_open(self, asset_class: AssetClass | str | None = None) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        return None


class PaperBroker(BrokerInterface):
    """Simple in-memory broker for paper trading and testing."""

    def __init__(
        self,
        settings: Settings | None = None,
        market_data_service: MarketDataService | None = None,
    ):
        self.settings = settings or get_settings()
        self.market_data_service = market_data_service or CSVMarketDataService()
        self.starting_cash = 100_000.0
        self.cash = 100_000.0
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def get_account(self) -> BrokerAccount:
        with self._lock:
            equity = self.calculate_equity()
            return BrokerAccount(
                cash=self.cash,
                equity=equity,
                positions=len(self.positions),
                buying_power=self.cash,
                mode=self.settings.broker_mode,
                trading_enabled=self.settings.trading_enabled,
            )

    def list_assets(self, asset_class: AssetClass | str | None = None) -> list[AssetMetadata]:
        assets = []
        list_supported_assets = getattr(self.market_data_service, "list_supported_assets", None)
        if callable(list_supported_assets):
            assets = list_supported_assets()
        resolved_class = normalize_asset_class(asset_class)
        if resolved_class == AssetClass.UNKNOWN:
            return assets
        return [asset for asset in assets if asset.asset_class == resolved_class]

    def get_asset(self, symbol: str, asset_class: AssetClass | str | None = None) -> AssetMetadata | None:
        target_symbol = canonicalize_symbol(symbol, asset_class)
        for asset in self.list_assets(asset_class):
            if canonicalize_symbol(asset.symbol, asset.asset_class) == target_symbol:
                return asset
        return None

    def get_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    **pos.copy(),
                    "qty": pos["quantity"],
                    "avg_entry_price": pos["entry_price"],
                    "market_value": pos["quantity"] * pos["current_price"],
                }
                for pos in self.positions.values()
            ]

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        if not self.settings.is_mock_mode:
            raise RuntimeError("PaperBroker only supports BROKER_MODE=mock.")

        with self._lock:
            resolved_asset_class = (
                order.asset_class
                if order.asset_class != AssetClass.UNKNOWN
                else infer_asset_class(order.symbol)
            )
            price = order.price or self.get_latest_price(order.symbol, resolved_asset_class)
            filled_qty = order.quantity or (
                (order.notional / price) if order.notional is not None and price > 0 else 0.0
            )
            result = {
                "id": f"paper-{len(self.orders) + 1}",
                "symbol": canonicalize_symbol(order.symbol, resolved_asset_class),
                "asset_class": resolved_asset_class.value,
                "side": order.side.value if hasattr(order.side, "value") else str(order.side),
                "quantity": filled_qty,
                "notional": order.notional,
                "price": price,
                "status": "FILLED" if not order.is_dry_run else "DRY_RUN",
                "executed_at": datetime.datetime.utcnow().isoformat(),
                "is_dry_run": order.is_dry_run,
                "time_in_force": order.time_in_force or self._default_tif(resolved_asset_class),
            }
            self.orders.append(result)

            if not order.is_dry_run:
                self.apply_trade(result["symbol"], resolved_asset_class, result["side"], filled_qty, price)

            return result

    def _default_tif(self, asset_class: AssetClass) -> str:
        return "gtc" if asset_class == AssetClass.CRYPTO else "day"

    def is_market_open(self, asset_class: AssetClass | str | None = None) -> bool:
        session = self.market_data_service.get_session_status(asset_class or AssetClass.EQUITY)
        return session.is_open

    def apply_trade(
        self,
        symbol: str,
        asset_class: AssetClass,
        side: str,
        quantity: float,
        price: float,
    ) -> None:
        normalized_side = side.value if hasattr(side, "value") else str(side)
        normalized_side = normalized_side.upper()
        if normalized_side == "BUY":
            existing = self.positions.get(symbol)
            if existing:
                total_quantity = existing["quantity"] + quantity
                average_entry_price = (
                    (existing["quantity"] * existing["entry_price"]) + (quantity * price)
                ) / total_quantity
            else:
                total_quantity = quantity
                average_entry_price = price

            self.cash -= quantity * price
            self.positions[symbol] = {
                "symbol": symbol,
                "asset_class": asset_class.value,
                "exchange": "MOCK",
                "quantity": total_quantity,
                "entry_price": average_entry_price,
                "side": normalized_side,
                "current_price": price,
            }
            return

        if symbol not in self.positions:
            return

        position = self.positions[symbol]
        remaining_quantity = position["quantity"] - quantity
        self.cash += quantity * price
        if remaining_quantity <= 0:
            self.positions.pop(symbol, None)
            return

        position["quantity"] = remaining_quantity
        position["current_price"] = price

    def close_position(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            if symbol not in self.positions:
                return {"message": "No position to close", "symbol": symbol}

            position = self.positions[symbol]
            price = self.get_latest_price(symbol, position.get("asset_class"))
            self.cash += position["quantity"] * price
            self.positions.pop(symbol, None)
            return {
                "symbol": symbol,
                "closed_at": datetime.datetime.utcnow().isoformat(),
                "price": price,
            }

    def close_all_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            symbols = list(self.positions.keys())
        return [self.close_position(symbol) for symbol in symbols]

    def list_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return [order.copy() for order in self.orders]

    def cancel_open_orders(self) -> list[dict[str, Any]]:
        canceled_orders: list[dict[str, Any]] = []
        with self._lock:
            for order in self.orders:
                if str(order.get("status", "")).upper() in {"NEW", "OPEN", "ACCEPTED", "PENDING"}:
                    order["status"] = "CANCELED"
                    canceled_orders.append(order.copy())
        return canceled_orders

    def reset_state(
        self,
        *,
        clear_orders: bool = True,
        clear_positions: bool = False,
    ) -> None:
        with self._lock:
            if clear_positions:
                self.positions.clear()
                self.cash = self.starting_cash
            if clear_orders:
                self.orders.clear()

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        return self.market_data_service.get_latest_price(symbol, asset_class)

    def calculate_equity(self) -> float:
        total = self.cash
        for position in self.positions.values():
            total += position["quantity"] * position["current_price"]
        return total

    def close(self) -> None:
        return None


class AlpacaBroker(BrokerInterface):
    """Adapter for Alpaca paper and live trading APIs."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.is_alpaca_mode:
            raise RuntimeError("AlpacaBroker is only available when BROKER_MODE=paper.")
        if not self.settings.has_alpaca_credentials:
            raise ValueError("Alpaca trading requires ALPACA_API_KEY and ALPACA_SECRET_KEY.")

        self.base_url = str(self.settings.alpaca_base_url).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            timeout=10.0,
        )
        self.market_data_client = httpx.Client(
            base_url=str(self.settings.alpaca_data_base_url).rstrip("/"),
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            timeout=10.0,
        )
        self._asset_cache: dict[str, AssetMetadata] = {}

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self.client.request(method, path, params=params, json=json)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise BrokerAuthError(
                    "Alpaca authentication failed. Check ALPACA_API_KEY, ALPACA_SECRET_KEY, and ALPACA_BASE_URL."
                ) from exc
            raise BrokerUpstreamError(
                f"Alpaca API returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise BrokerConnectionError(f"Failed to connect to Alpaca API: {exc}") from exc

    def _ensure_paper_admin_action_allowed(self) -> None:
        if self.settings.is_live_enabled:
            raise BrokerError("Administrative broker reset actions are blocked while live trading is enabled.")

    def is_market_open(self, asset_class: AssetClass | str | None = None) -> bool:
        resolved_class = normalize_asset_class(asset_class)
        if resolved_class == AssetClass.CRYPTO:
            return True
        try:
            data = self._request("GET", "/v2/clock")
            return bool(data.get("is_open", False))
        except BrokerError:
            return False

    def get_account(self) -> BrokerAccount:
        data = self._request("GET", "/v2/account")
        try:
            positions_data = self._request("GET", "/v2/positions")
            positions_count = len(positions_data) if isinstance(positions_data, list) else 0
        except BrokerError:
            positions_count = 0
        return BrokerAccount(
            cash=float(data.get("cash", 0.0)),
            equity=float(data.get("equity", 0.0)),
            positions=positions_count,
            buying_power=float(data.get("buying_power", 0.0)),
            mode=self.settings.broker_mode,
            trading_enabled=self.settings.trading_enabled,
            currency=str(data.get("currency", "USD")),
        )

    def _map_asset(self, raw_asset: dict[str, Any]) -> AssetMetadata:
        symbol = raw_asset.get("symbol", "")
        raw_class = str(raw_asset.get("class") or raw_asset.get("asset_class") or "")
        if raw_class.lower() == "crypto":
            asset_class = AssetClass.CRYPTO
        else:
            asset_class = infer_asset_class(symbol, raw_asset.get("name"))
        attributes = raw_asset.get("attributes") or []
        if not isinstance(attributes, list):
            attributes = [str(attributes)]
        metadata = AssetMetadata(
            symbol=canonicalize_symbol(symbol, asset_class),
            name=str(raw_asset.get("name") or symbol),
            asset_class=asset_class,
            exchange=raw_asset.get("exchange"),
            status=str(raw_asset.get("status", "active")),
            tradable=bool(raw_asset.get("tradable", True)),
            fractionable=bool(raw_asset.get("fractionable", False)),
            shortable=bool(raw_asset.get("shortable", False)),
            easy_to_borrow=bool(raw_asset.get("easy_to_borrow", False)),
            marginable=bool(raw_asset.get("marginable", False)),
            attributes=[str(item) for item in attributes],
            raw=raw_asset,
        )
        self._asset_cache[metadata.symbol] = metadata
        return metadata

    def list_assets(self, asset_class: AssetClass | str | None = None) -> list[AssetMetadata]:
        requested_class = normalize_asset_class(asset_class)
        classes: list[AssetClass]
        if requested_class == AssetClass.UNKNOWN:
            classes = [AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO]
        else:
            classes = [requested_class]

        assets: list[AssetMetadata] = []
        fetched_symbols: set[str] = set()

        for current_class in classes:
            if current_class == AssetClass.OPTION:
                continue
            params = {"status": "active"}
            if current_class == AssetClass.CRYPTO:
                params["asset_class"] = "crypto"
            else:
                params["asset_class"] = "us_equity"
            response = self._request("GET", "/v2/assets", params=params)
            if not isinstance(response, list):
                continue
            for raw_asset in response:
                metadata = self._map_asset(raw_asset)
                if requested_class != AssetClass.UNKNOWN and metadata.asset_class != requested_class:
                    continue
                if metadata.symbol in fetched_symbols:
                    continue
                fetched_symbols.add(metadata.symbol)
                assets.append(metadata)
        return assets

    def get_asset(self, symbol: str, asset_class: AssetClass | str | None = None) -> AssetMetadata | None:
        resolved_symbol = canonicalize_symbol(symbol, asset_class)
        cached = self._asset_cache.get(resolved_symbol)
        if cached is not None:
            return cached
        try:
            response = self._request("GET", f"/v2/assets/{resolved_symbol}")
        except BrokerError:
            return None
        if not isinstance(response, dict):
            return None
        return self._map_asset(response)

    def get_positions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/positions")
        if not isinstance(response, list):
            return []
        positions: list[dict[str, Any]] = []
        for item in response:
            symbol = canonicalize_symbol(item.get("symbol", ""), item.get("asset_class"))
            asset = self._asset_cache.get(symbol) or self.get_asset(symbol, item.get("asset_class"))
            positions.append(
                {
                    **item,
                    "symbol": symbol,
                    "asset_class": (asset.asset_class.value if asset else infer_asset_class(symbol).value),
                    "exchange": asset.exchange if asset else None,
                    "quantity": float(item.get("qty", item.get("quantity", 0.0))),
                    "current_price": float(item.get("current_price", item.get("lastday_price", 0.0) or 0.0)),
                    "entry_price": float(item.get("avg_entry_price", item.get("entry_price", 0.0))),
                    "market_value": float(item.get("market_value", 0.0)),
                }
            )
        return positions

    def submit_order(self, order: OrderRequest) -> dict[str, Any]:
        resolved_asset_class = order.asset_class if order.asset_class != AssetClass.UNKNOWN else infer_asset_class(order.symbol)
        if order.is_dry_run:
            return {
                "symbol": canonicalize_symbol(order.symbol, resolved_asset_class),
                "asset_class": resolved_asset_class.value,
                "side": order.side.value if hasattr(order.side, "value") else str(order.side),
                "quantity": order.quantity,
                "notional": order.notional,
                "price": order.price,
                "status": "DRY_RUN",
                "is_dry_run": True,
                "time_in_force": order.time_in_force or self._default_tif(resolved_asset_class),
            }

        payload: dict[str, Any] = {
            "symbol": canonicalize_symbol(order.symbol, resolved_asset_class),
            "side": str(order.side).lower(),
            "type": order.order_type,
            "time_in_force": order.time_in_force or self._default_tif(resolved_asset_class),
        }
        if order.notional is not None:
            payload["notional"] = order.notional
        elif order.quantity is not None:
            payload["qty"] = order.quantity
        else:
            raise ValueError("OrderRequest must include quantity or notional.")
        return self._request("POST", "/v2/orders", json=payload)

    def _default_tif(self, asset_class: AssetClass) -> str:
        return "gtc" if asset_class == AssetClass.CRYPTO else "day"

    def close_position(self, symbol: str) -> dict[str, Any]:
        self._ensure_paper_admin_action_allowed()
        return self._request("DELETE", f"/v2/positions/{symbol}")

    def close_all_positions(self) -> list[dict[str, Any]]:
        self._ensure_paper_admin_action_allowed()
        return [self.close_position(position["symbol"]) for position in self.get_positions()]

    def list_orders(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v2/orders", params={"status": "all", "limit": 100})
        if not isinstance(response, list):
            return []
        return response

    def cancel_open_orders(self) -> list[dict[str, Any]]:
        self._ensure_paper_admin_action_allowed()
        response = self._request("GET", "/v2/orders", params={"status": "open", "limit": 100})
        if not isinstance(response, list):
            return []

        canceled_orders: list[dict[str, Any]] = []
        for order in response:
            order_id = order.get("id")
            if not order_id:
                continue
            self._request("DELETE", f"/v2/orders/{order_id}")
            canceled_orders.append(
                {
                    "id": order_id,
                    "symbol": order.get("symbol"),
                    "status": "CANCELED",
                }
            )
        return canceled_orders

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.CRYPTO:
            try:
                response = self.market_data_client.get(
                    f"/v1beta3/crypto/{self.settings.alpaca_crypto_location}/latest/trades",
                    params={"symbols": canonicalize_symbol(symbol, AssetClass.CRYPTO)},
                )
                response.raise_for_status()
                payload = response.json()
                trade = (payload.get("trades") or {}).get(canonicalize_symbol(symbol, AssetClass.CRYPTO)) or {}
                return float(trade.get("p", 0.0))
            except httpx.HTTPStatusError as exc:
                raise BrokerUpstreamError(
                    f"Alpaca crypto market data error {exc.response.status_code}: {exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                raise BrokerConnectionError(f"Failed to connect to Alpaca market data: {exc}") from exc

        try:
            response = self.market_data_client.get(
                f"/v2/stocks/{canonicalize_symbol(symbol, resolved_asset_class)}/trades/latest",
                params={"feed": "iex"},
            )
            response.raise_for_status()
            payload = response.json()
            trade = payload.get("trade") or {}
            return float(trade.get("p", 0.0))
        except httpx.HTTPStatusError as exc:
            raise BrokerUpstreamError(
                f"Alpaca market data error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            raise BrokerConnectionError(f"Failed to connect to Alpaca market data: {exc}") from exc

    def close(self) -> None:
        self.client.close()
        self.market_data_client.close()


def create_broker(
    settings: Settings | None = None,
    market_data_service: MarketDataService | None = None,
) -> BrokerInterface:
    settings = settings or get_settings()
    mode = settings.broker_mode.lower()
    if mode == "mock":
        return PaperBroker(settings, market_data_service=market_data_service or CSVMarketDataService())
    if mode == "paper":
        return AlpacaBroker(settings)
    raise ValueError(f"Unsupported broker mode '{settings.broker_mode}'.")
