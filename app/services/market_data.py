from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.config.settings import Settings, get_settings
from app.domain.models import (
    AssetClass,
    AssetMetadata,
    MarketSessionStatus,
    NormalizedBar,
    QuoteSnapshot,
    SessionState,
    TradeSnapshot,
)
from app.monitoring.logger import get_logger

logger = get_logger("market_data")
NY_TZ = ZoneInfo("America/New_York")

MOCK_ASSET_NAMES: dict[str, tuple[str, AssetClass]] = {
    "AAPL": ("Apple Inc.", AssetClass.EQUITY),
    "SPY": ("SPDR S&P 500 ETF Trust", AssetClass.ETF),
    "QQQ": ("Invesco QQQ Trust", AssetClass.ETF),
    "BTC/USD": ("Bitcoin / US Dollar", AssetClass.CRYPTO),
    "ETH/USD": ("Ethereum / US Dollar", AssetClass.CRYPTO),
}


class MarketDataService(Protocol):
    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass | str,
        timeframe: str,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[NormalizedBar]:
        ...

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        ...

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        ...

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        ...

    def batch_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass | str,
    ) -> dict[str, dict[str, Any]]:
        ...

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        ...

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        ...

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        ...


def normalize_asset_class(value: AssetClass | str | None) -> AssetClass:
    if isinstance(value, AssetClass):
        return value
    if value is None:
        return AssetClass.UNKNOWN
    normalized = str(value).strip().lower()
    try:
        return AssetClass(normalized)
    except ValueError:
        return AssetClass.UNKNOWN


def infer_asset_class(symbol: str, name: str | None = None) -> AssetClass:
    normalized_symbol = symbol.strip().upper()
    normalized_name = (name or "").upper()
    if "/" in normalized_symbol or normalized_symbol.endswith("USD") and len(normalized_symbol) <= 10:
        if normalized_symbol.startswith(("BTC", "ETH", "SOL", "DOGE", "LTC")):
            return AssetClass.CRYPTO
    if "ETF" in normalized_name or normalized_symbol in {"SPY", "QQQ", "DIA", "IWM", "VTI", "IVV"}:
        return AssetClass.ETF
    return AssetClass.EQUITY


def canonicalize_symbol(symbol: str, asset_class: AssetClass | str | None = None) -> str:
    normalized_symbol = symbol.strip().upper()
    resolved_class = normalize_asset_class(asset_class)
    if resolved_class == AssetClass.CRYPTO:
        if "/" in normalized_symbol:
            return normalized_symbol
        if normalized_symbol.endswith("USD") and len(normalized_symbol) > 3:
            return f"{normalized_symbol[:-3]}/USD"
    return normalized_symbol


def bars_to_dataframe(bars: list[NormalizedBar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return pd.DataFrame(
        [
            {
                "Date": bar.timestamp,
                "Open": bar.open,
                "High": bar.high,
                "Low": bar.low,
                "Close": bar.close,
                "Volume": bar.volume,
                "TradeCount": bar.trade_count,
                "VWAP": bar.vwap,
            }
            for bar in bars
        ]
    ).sort_values("Date").reset_index(drop=True)


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _timeframe_to_timedelta(timeframe: str) -> timedelta:
    normalized = timeframe.strip().upper()
    mapping = {
        "1MIN": timedelta(minutes=1),
        "1T": timedelta(minutes=1),
        "5MIN": timedelta(minutes=5),
        "5T": timedelta(minutes=5),
        "15MIN": timedelta(minutes=15),
        "15T": timedelta(minutes=15),
        "1H": timedelta(hours=1),
        "1HOUR": timedelta(hours=1),
        "4H": timedelta(hours=4),
        "1D": timedelta(days=1),
        "1DAY": timedelta(days=1),
    }
    return mapping.get(normalized, timedelta(days=1))


def _session_status_for(asset_class: AssetClass) -> MarketSessionStatus:
    now = datetime.now(timezone.utc)
    if asset_class == AssetClass.CRYPTO:
        return MarketSessionStatus(
            asset_class=asset_class,
            is_open=True,
            session_state=SessionState.ALWAYS_OPEN,
            extended_hours=False,
            is_24_7=True,
            as_of=now,
        )

    eastern_now = now.astimezone(NY_TZ)
    is_weekday = eastern_now.weekday() < 5
    current_minutes = eastern_now.hour * 60 + eastern_now.minute

    premarket_start = 4 * 60
    regular_start = 9 * 60 + 30
    regular_end = 16 * 60
    postmarket_end = 20 * 60

    if not is_weekday:
        session_state = SessionState.CLOSED
        is_open = False
    elif premarket_start <= current_minutes < regular_start:
        session_state = SessionState.PREMARKET
        is_open = True
    elif regular_start <= current_minutes < regular_end:
        session_state = SessionState.REGULAR
        is_open = True
    elif regular_end <= current_minutes < postmarket_end:
        session_state = SessionState.POSTMARKET
        is_open = True
    else:
        session_state = SessionState.CLOSED
        is_open = False

    return MarketSessionStatus(
        asset_class=asset_class,
        is_open=is_open,
        session_state=session_state,
        extended_hours=session_state in {SessionState.PREMARKET, SessionState.POSTMARKET},
        is_24_7=False,
        as_of=now,
    )


class CSVMarketDataService:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)

    def list_supported_symbols(self) -> list[str]:
        if not self.data_dir.exists():
            return []

        discovered: list[str] = []
        for csv_path in sorted(self.data_dir.glob("*.csv")):
            stem = csv_path.stem.upper()
            if stem == "SAMPLE":
                continue
            if stem in {"BTCUSD", "ETHUSD"}:
                discovered.append(f"{stem[:-3]}/USD")
                continue
            discovered.append(stem)
        return discovered

    def list_supported_assets(self) -> list[AssetMetadata]:
        assets: list[AssetMetadata] = []
        for symbol in self.list_supported_symbols():
            name, asset_class = MOCK_ASSET_NAMES.get(
                symbol,
                (symbol, infer_asset_class(symbol)),
            )
            assets.append(
                AssetMetadata(
                    symbol=symbol,
                    name=name,
                    asset_class=asset_class,
                    exchange="MOCK",
                    tradable=True,
                    fractionable=asset_class in {AssetClass.ETF, AssetClass.CRYPTO},
                    shortable=asset_class != AssetClass.CRYPTO,
                    easy_to_borrow=asset_class != AssetClass.CRYPTO,
                    marginable=asset_class != AssetClass.CRYPTO,
                    attributes=["mock_data"],
                    raw={"source": "csv"},
                )
            )
        return assets

    def _symbol_filename_candidates(self, symbol: str) -> list[Path]:
        normalized = symbol.strip().upper()
        compact = normalized.replace("/", "")
        lower_compact = compact.lower()
        return [
            self.data_dir / f"{normalized}.csv",
            self.data_dir / f"{normalized.lower()}.csv",
            self.data_dir / f"{compact}.csv",
            self.data_dir / f"{lower_compact}.csv",
        ]

    def resolve_csv_path(self, symbol: str) -> Path:
        for path in self._symbol_filename_candidates(symbol):
            if path.exists():
                return path

        supported = self.list_supported_symbols()
        supported_text = ", ".join(supported) if supported else "no symbol CSV files are available"
        raise FileNotFoundError(
            f"Mock market data for symbol '{symbol.strip().upper()}' was not found. "
            f"Add a symbol CSV under '{self.data_dir}' or choose one of: {supported_text}."
        )

    def load_historical(self, csv_path: Path) -> pd.DataFrame:
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV path not found: {csv_path}")
        df = pd.read_csv(csv_path, parse_dates=["Date"])
        return df.sort_values("Date").reset_index(drop=True)

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass | str,
        timeframe: str,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[NormalizedBar]:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)

        csv_path = self.resolve_csv_path(symbol)
        df = self.load_historical(csv_path)
        if start is not None:
            df = df[df["Date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["Date"] <= pd.Timestamp(end)]
        if limit and len(df) > limit:
            df = df.tail(limit)

        bars: list[NormalizedBar] = []
        for _, row in df.iterrows():
            bars.append(
                NormalizedBar(
                    symbol=canonicalize_symbol(symbol, resolved_asset_class),
                    asset_class=resolved_asset_class,
                    timestamp=_parse_timestamp(row["Date"]),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                )
            )
        return bars

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        bars = self.get_bars(
            symbol=symbol,
            asset_class=asset_class or infer_asset_class(symbol),
            timeframe=timeframe or "1D",
            limit=limit,
        )
        return bars_to_dataframe(bars)

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        bars = self.get_bars(symbol, asset_class, timeframe="1D", limit=1)
        if not bars:
            raise RuntimeError(f"No trade snapshot available for symbol {symbol}")
        latest = bars[-1]
        return TradeSnapshot(
            symbol=latest.symbol,
            asset_class=latest.asset_class,
            price=latest.close,
            size=latest.volume,
            timestamp=latest.timestamp,
        )

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        trade = self.get_latest_trade(symbol, asset_class)
        spread_basis = 0.0015 if trade.asset_class == AssetClass.CRYPTO else 0.0008
        price = trade.price or 0.0
        return QuoteSnapshot(
            symbol=trade.symbol,
            asset_class=trade.asset_class,
            bid_price=price * (1 - spread_basis / 2),
            ask_price=price * (1 + spread_basis / 2),
            bid_size=trade.size,
            ask_size=trade.size,
            timestamp=trade.timestamp,
        )

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        bars = self.get_bars(symbol, asset_class, timeframe="1D", limit=2)
        trade = self.get_latest_trade(symbol, asset_class)
        quote = self.get_latest_quote(symbol, asset_class)
        previous_close = bars[-2].close if len(bars) > 1 else trade.price
        daily_change_pct = None
        if previous_close and previous_close > 0 and trade.price is not None:
            daily_change_pct = (trade.price - previous_close) / previous_close
        session = self.get_session_status(asset_class)
        return {
            "symbol": canonicalize_symbol(symbol, asset_class),
            "asset_class": normalize_asset_class(asset_class).value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": session.to_dict(),
            "daily_change_pct": daily_change_pct,
            "latest_bar": bars[-1].to_dict() if bars else None,
        }

    def batch_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass | str,
    ) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            try:
                snapshots[canonicalize_symbol(symbol, asset_class)] = self.get_snapshot(symbol, asset_class)
            except Exception as exc:
                logger.warning("Snapshot unavailable for %s: %s", symbol, exc)
        return snapshots

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        return _session_status_for(resolved_asset_class)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        trade = self.get_latest_trade(symbol, asset_class or infer_asset_class(symbol))
        if trade.price is None:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(trade.price)


class AlpacaMarketDataService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.has_alpaca_credentials:
            raise ValueError("Alpaca market data requires ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        self.base_url = str(self.settings.alpaca_data_base_url).rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
            },
            timeout=10.0,
        )

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < 2:
                    continue
                raise RuntimeError(
                    f"Alpaca market data error {exc.response.status_code}: {exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 2:
                    raise RuntimeError(f"Alpaca market data request failed: {exc}") from exc
        raise RuntimeError(f"Alpaca market data request failed: {last_error}")

    def _api_timeframe(self, timeframe: str) -> str:
        return timeframe

    def _compute_historical_window(self, timeframe: str, limit: int) -> tuple[str, str]:
        end_date = datetime.utcnow()
        delta = _timeframe_to_timedelta(timeframe)
        buffer_multiplier = 2.5 if delta >= timedelta(days=1) else 3.0
        start_date = end_date - (delta * max(limit, 2) * buffer_multiplier)
        return start_date.strftime("%Y-%m-%dT%H:%M:%SZ"), end_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_stock_bars(self, payload: dict[str, Any], symbol: str, asset_class: AssetClass) -> list[NormalizedBar]:
        bars = payload.get("bars") or []
        if not bars:
            raise RuntimeError(f"No bar data returned for symbol {symbol}")
        return [
            NormalizedBar(
                symbol=canonicalize_symbol(symbol, asset_class),
                asset_class=asset_class,
                timestamp=_parse_timestamp(item.get("t")),
                open=float(item.get("o", 0.0)),
                high=float(item.get("h", 0.0)),
                low=float(item.get("l", 0.0)),
                close=float(item.get("c", 0.0)),
                volume=float(item.get("v", 0.0)),
                trade_count=int(item["n"]) if item.get("n") is not None else None,
                vwap=float(item["vw"]) if item.get("vw") is not None else None,
            )
            for item in bars
        ]

    def _parse_crypto_bars(self, payload: dict[str, Any], symbol: str) -> list[NormalizedBar]:
        normalized_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        grouped = payload.get("bars") or {}
        bars = grouped.get(normalized_symbol) or grouped.get(normalized_symbol.replace("/", "")) or []
        if not bars:
            raise RuntimeError(f"No crypto bar data returned for symbol {normalized_symbol}")
        return [
            NormalizedBar(
                symbol=normalized_symbol,
                asset_class=AssetClass.CRYPTO,
                timestamp=_parse_timestamp(item.get("t")),
                open=float(item.get("o", 0.0)),
                high=float(item.get("h", 0.0)),
                low=float(item.get("l", 0.0)),
                close=float(item.get("c", 0.0)),
                volume=float(item.get("v", 0.0)),
                trade_count=int(item["n"]) if item.get("n") is not None else None,
                vwap=float(item["vw"]) if item.get("vw") is not None else None,
            )
            for item in bars
        ]

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass | str,
        timeframe: str,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[NormalizedBar]:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        start_iso, end_iso = self._compute_historical_window(timeframe, limit)
        params = {
            "timeframe": self._api_timeframe(timeframe),
            "limit": limit,
            "sort": "asc",
            "start": start.isoformat() if start else start_iso,
            "end": end.isoformat() if end else end_iso,
        }

        if resolved_asset_class == AssetClass.CRYPTO:
            payload = self._request_json(
                f"/v1beta3/crypto/{self.settings.alpaca_crypto_location}/bars",
                params={**params, "symbols": resolved_symbol},
            )
            return self._parse_crypto_bars(payload, resolved_symbol)

        payload = self._request_json(
            f"/v2/stocks/{resolved_symbol}/bars",
            params={**params, "feed": "iex"},
        )
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(resolved_symbol)
        return self._parse_stock_bars(payload, resolved_symbol, resolved_asset_class)

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        bars = self.get_bars(
            symbol=symbol,
            asset_class=asset_class or infer_asset_class(symbol),
            timeframe=timeframe or self.settings.default_timeframe,
            limit=limit,
        )
        return bars_to_dataframe(bars)

    def _parse_stock_quote(self, payload: dict[str, Any], symbol: str, asset_class: AssetClass) -> QuoteSnapshot:
        quote = payload.get("quote") or payload
        return QuoteSnapshot(
            symbol=canonicalize_symbol(symbol, asset_class),
            asset_class=asset_class,
            ask_price=float(quote["ap"]) if quote.get("ap") is not None else None,
            ask_size=float(quote["as"]) if quote.get("as") is not None else None,
            bid_price=float(quote["bp"]) if quote.get("bp") is not None else None,
            bid_size=float(quote["bs"]) if quote.get("bs") is not None else None,
            timestamp=_parse_timestamp(quote.get("t")) if quote.get("t") else None,
        )

    def _parse_trade(self, payload: dict[str, Any], symbol: str, asset_class: AssetClass) -> TradeSnapshot:
        trade = payload.get("trade") or payload
        return TradeSnapshot(
            symbol=canonicalize_symbol(symbol, asset_class),
            asset_class=asset_class,
            price=float(trade["p"]) if trade.get("p") is not None else None,
            size=float(trade["s"]) if trade.get("s") is not None else None,
            timestamp=_parse_timestamp(trade.get("t")) if trade.get("t") else None,
        )

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        if resolved_asset_class == AssetClass.CRYPTO:
            payload = self._request_json(
                f"/v1beta3/crypto/{self.settings.alpaca_crypto_location}/latest/orderbooks",
                params={"symbols": resolved_symbol},
            )
            orderbooks = payload.get("orderbooks") or {}
            orderbook = orderbooks.get(resolved_symbol) or {}
            asks = orderbook.get("a") or []
            bids = orderbook.get("b") or []
            best_ask = asks[0] if asks else {}
            best_bid = bids[0] if bids else {}
            return QuoteSnapshot(
                symbol=resolved_symbol,
                asset_class=AssetClass.CRYPTO,
                ask_price=float(best_ask["p"]) if best_ask.get("p") is not None else None,
                ask_size=float(best_ask["s"]) if best_ask.get("s") is not None else None,
                bid_price=float(best_bid["p"]) if best_bid.get("p") is not None else None,
                bid_size=float(best_bid["s"]) if best_bid.get("s") is not None else None,
                timestamp=datetime.now(timezone.utc),
            )

        payload = self._request_json(f"/v2/stocks/{resolved_symbol}/quotes/latest", params={"feed": "iex"})
        return self._parse_stock_quote(payload, resolved_symbol, resolved_asset_class or infer_asset_class(resolved_symbol))

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        if resolved_asset_class == AssetClass.CRYPTO:
            payload = self._request_json(
                f"/v1beta3/crypto/{self.settings.alpaca_crypto_location}/latest/trades",
                params={"symbols": resolved_symbol},
            )
            trades = payload.get("trades") or {}
            trade = trades.get(resolved_symbol) or {}
            return self._parse_trade(trade, resolved_symbol, AssetClass.CRYPTO)

        payload = self._request_json(f"/v2/stocks/{resolved_symbol}/trades/latest", params={"feed": "iex"})
        return self._parse_trade(payload, resolved_symbol, resolved_asset_class or infer_asset_class(resolved_symbol))

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        if resolved_asset_class == AssetClass.CRYPTO:
            bars = self.get_bars(resolved_symbol, resolved_asset_class, timeframe="1D", limit=2)
            daily_bar = bars[-1].to_dict() if bars else {}
            snapshot: dict[str, Any] = {}
        else:
            snapshot = self._request_json(f"/v2/stocks/{resolved_symbol}/snapshot", params={"feed": "iex"})
            daily_bar = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}

        quote = self.get_latest_quote(resolved_symbol, resolved_asset_class)
        trade = self.get_latest_trade(resolved_symbol, resolved_asset_class)
        session = self.get_session_status(resolved_asset_class)
        prev_close = float(daily_bar.get("o", daily_bar.get("open", 0.0))) or None
        daily_change_pct = None
        if prev_close and prev_close > 0 and trade.price is not None:
            daily_change_pct = (trade.price - prev_close) / prev_close

        return {
            "symbol": resolved_symbol,
            "asset_class": resolved_asset_class.value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": session.to_dict(),
            "daily_change_pct": daily_change_pct,
            "latest_bar": snapshot.get("latestBar") or snapshot.get("minuteBar") or snapshot.get("minute_bar"),
            "daily_bar": daily_bar,
        }

    def batch_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass | str,
    ) -> dict[str, dict[str, Any]]:
        resolved_asset_class = normalize_asset_class(asset_class)
        if not symbols:
            return {}
        if resolved_asset_class == AssetClass.CRYPTO:
            results: dict[str, dict[str, Any]] = {}
            for symbol in symbols:
                try:
                    canonical_symbol = canonicalize_symbol(symbol, resolved_asset_class)
                    results[canonical_symbol] = self.get_snapshot(canonical_symbol, resolved_asset_class)
                except Exception as exc:
                    logger.warning("Crypto snapshot unavailable for %s: %s", symbol, exc)
            return results

        joined_symbols = ",".join(canonicalize_symbol(symbol, resolved_asset_class) for symbol in symbols)
        payload = self._request_json("/v2/stocks/snapshots", params={"symbols": joined_symbols, "feed": "iex"})
        snapshots = payload.get("snapshots") or payload
        return {
            symbol: {
                "symbol": symbol,
                "asset_class": resolved_asset_class.value,
                "snapshot": data,
            }
            for symbol, data in snapshots.items()
        }

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        return _session_status_for(resolved_asset_class)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        bars = self.fetch_bars(
            symbol,
            asset_class=asset_class or infer_asset_class(symbol),
            timeframe=self.settings.default_timeframe,
            limit=1,
        )
        if bars.empty:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(bars.iloc[-1]["Close"])

    def close(self) -> None:
        self.client.close()
