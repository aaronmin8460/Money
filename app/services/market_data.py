from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
    NormalizedMarketSnapshot,
    NormalizedBar,
    QuoteSnapshot,
    SessionState,
    TradeSnapshot,
)
from app.monitoring.logger import get_logger
from app.utils.datetime_parser import parse_iso_datetime

logger = get_logger("market_data")
NY_TZ = ZoneInfo("America/New_York")

MOCK_ASSET_NAMES: dict[str, tuple[str, AssetClass]] = {
    "AAPL": ("Apple Inc.", AssetClass.EQUITY),
    "SPY": ("SPDR S&P 500 ETF Trust", AssetClass.ETF),
    "QQQ": ("Invesco QQQ Trust", AssetClass.ETF),
    "BTC/USD": ("Bitcoin / US Dollar", AssetClass.CRYPTO),
    "ETH/USD": ("Ethereum / US Dollar", AssetClass.CRYPTO),
}


def _looks_like_crypto_symbol(symbol: str) -> bool:
    normalized = symbol.strip().upper()
    if not normalized:
        return False
    if "/" in normalized:
        base, quote = normalized.split("/", 1)
        return bool(base) and quote in {"USD", "USDT", "USDC", "BTC", "ETH"}
    return normalized.endswith(("USD", "USDT", "USDC")) and len(normalized) > 3


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

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
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

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        ...

    def diagnostics(self) -> dict[str, Any]:
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
    if _looks_like_crypto_symbol(normalized_symbol):
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
    fallback_timestamp = datetime.now(timezone.utc)
    parsed = parse_iso_datetime(value, default_none=fallback_timestamp)
    return parsed or fallback_timestamp


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


def _quote_valid(bid_price: float | None, ask_price: float | None) -> bool:
    if bid_price is None or ask_price is None:
        return False
    if bid_price <= 0 or ask_price <= 0:
        return False
    if bid_price > ask_price:
        return False
    return True


def _build_normalized_snapshot(
    *,
    symbol: str,
    asset_class: AssetClass,
    session: MarketSessionStatus,
    trade: TradeSnapshot,
    quote: QuoteSnapshot,
    quote_stale_after_seconds: int,
    source: str,
    fallback_price: float | None = None,
    exchange: str | None = None,
) -> NormalizedMarketSnapshot:
    bid_price = quote.bid_price
    ask_price = quote.ask_price
    quote_timestamp = quote.timestamp
    trade_timestamp = trade.timestamp

    quote_available = _quote_valid(bid_price, ask_price)
    mid_price = None
    spread_abs = None
    spread_pct = None
    quote_age_seconds = None
    quote_stale = False

    if quote_available:
        mid_price = ((ask_price or 0.0) + (bid_price or 0.0)) / 2
        spread_abs = max(0.0, (ask_price or 0.0) - (bid_price or 0.0))
        if mid_price and mid_price > 0:
            spread_pct = spread_abs / mid_price
        if quote_timestamp is not None and quote_stale_after_seconds > 0:
            quote_age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - quote_timestamp.astimezone(timezone.utc)).total_seconds(),
            )
            quote_stale = quote_age_seconds > quote_stale_after_seconds

    last_trade_price = trade.price
    fallback_used = False
    evaluation_price = last_trade_price
    price_source_used = "last_trade"
    if evaluation_price is None and mid_price is not None:
        evaluation_price = mid_price
        price_source_used = "mid_quote"
    if evaluation_price is None and fallback_price is not None:
        evaluation_price = fallback_price
        fallback_used = True
        price_source_used = "latest_bar_close_fallback"

    source_timestamp = quote_timestamp or trade_timestamp
    if source_timestamp is None and session.as_of is not None:
        source_timestamp = session.as_of

    return NormalizedMarketSnapshot(
        symbol=symbol,
        asset_class=asset_class,
        last_trade_price=last_trade_price,
        bid_price=bid_price,
        ask_price=ask_price,
        mid_price=mid_price,
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        quote_available=quote_available,
        quote_stale=quote_stale,
        quote_timestamp=quote_timestamp,
        trade_timestamp=trade_timestamp,
        source_timestamp=source_timestamp,
        quote_age_seconds=quote_age_seconds,
        fallback_pricing_used=fallback_used,
        price_source_used=price_source_used,
        evaluation_price=evaluation_price,
        session_state=session.session_state.value,
        exchange=exchange,
        source=source,
    )


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _timeframe_is_daily_or_slower(timeframe: str | None) -> bool:
    normalized = str(timeframe or "1D").strip().upper()
    return normalized in {"1D", "1DAY", "1W", "1WK", "1MO", "1M", "1MONTH"}


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    try:
        return max(0.0, float(stripped))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())


class TTLCache:
    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._items: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.expirations = 0

    def get(self, key: tuple[Any, ...]) -> Any | None:
        now = self._clock()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            expires_at, value = item
            if expires_at <= now:
                self.expirations += 1
                self.misses += 1
                self._items.pop(key, None)
                return None
            self.hits += 1
            return value

    def set(self, key: tuple[Any, ...], value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._items[key] = (self._clock() + ttl_seconds, value)
            self.sets += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._items),
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "expirations": self.expirations,
            }


class RequestLimiter:
    def __init__(
        self,
        provider_name: str,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.provider_name = provider_name
        self.requests_per_minute = max(0, int(requests_per_minute))
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._timestamps: deque[float] = deque()
        self._lock = threading.RLock()
        self.sleep_count = 0
        self.total_sleep_seconds = 0.0

    def wait(self, *, path: str | None = None) -> float:
        if self.requests_per_minute <= 0:
            return 0.0
        sleep_for = 0.0
        with self._lock:
            now = self._clock()
            window_start = now - 60.0
            while self._timestamps and self._timestamps[0] <= window_start:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.requests_per_minute:
                sleep_for = max(0.0, 60.0 - (now - self._timestamps[0]))
                self.sleep_count += 1
                self.total_sleep_seconds += sleep_for
            self._timestamps.append(now + sleep_for)
        if sleep_for > 0:
            logger.debug(
                "Market data provider rate-limit sleep",
                extra={
                    "provider": self.provider_name,
                    "path": path,
                    "sleep_seconds": sleep_for,
                    "requests_per_minute": self.requests_per_minute,
                },
            )
            self._sleeper(sleep_for)
        return sleep_for

    def stats(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "requests_per_minute": self.requests_per_minute,
            "sleep_count": self.sleep_count,
            "total_sleep_seconds": self.total_sleep_seconds,
        }


class MarketDataProviderBase:
    provider_name = "base"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: TTLCache | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.settings = settings or get_settings()
        self.cache = cache or TTLCache()
        self._sleep = sleeper or time.sleep
        rate_limit = int(self.settings.provider_rate_limits_per_minute.get(self.provider_name, 0) or 0)
        self.limiter = RequestLimiter(self.provider_name, rate_limit, sleeper=self._sleep)
        self._recent_429_count = 0
        self._recent_fallback_count = 0

    def _cache_key(self, *parts: Any) -> tuple[Any, ...]:
        return (self.provider_name, *parts)

    def _cached(self, key: tuple[Any, ...]) -> Any | None:
        value = self.cache.get(key)
        logger.debug(
            "Market data cache %s",
            "hit" if value is not None else "miss",
            extra={"provider": self.provider_name, "cache_key": key[1:]},
        )
        return value

    def _set_cache(self, key: tuple[Any, ...], value: Any, ttl_seconds: float) -> None:
        self.cache.set(key, value, ttl_seconds)

    def _bars_cache_ttl(self, timeframe: str | None) -> float:
        if _timeframe_is_daily_or_slower(timeframe):
            return float(self.settings.daily_bars_cache_ttl_seconds)
        return float(self.settings.intraday_bars_cache_ttl_seconds)

    def _normalized_from_price(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        price: float | None,
        timestamp: datetime | None = None,
        source: str | None = None,
        exchange: str | None = None,
        quote: QuoteSnapshot | None = None,
        volume: float | None = None,
    ) -> NormalizedMarketSnapshot:
        resolved_symbol = canonicalize_symbol(symbol, asset_class)
        timestamp = timestamp or datetime.now(timezone.utc)
        trade = TradeSnapshot(
            symbol=resolved_symbol,
            asset_class=asset_class,
            price=price,
            size=volume,
            timestamp=timestamp,
        )
        quote = quote or QuoteSnapshot(symbol=resolved_symbol, asset_class=asset_class, timestamp=timestamp)
        return _build_normalized_snapshot(
            symbol=resolved_symbol,
            asset_class=asset_class,
            session=self.get_session_status(asset_class),
            trade=trade,
            quote=quote,
            quote_stale_after_seconds=self.settings.quote_stale_after_seconds,
            source=source or self.provider_name,
            fallback_price=price,
            exchange=exchange,
        )

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        raise NotImplementedError(f"{self.provider_name} does not support option chains.")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "cache": self.cache.stats(),
            "rate_limiter": self.limiter.stats(),
            "recent_429_count": self._recent_429_count,
            "recent_fallback_count": self._recent_fallback_count,
        }


class CSVMarketDataService:
    def __init__(self, data_dir: str | Path = "data", settings: Settings | None = None):
        self.data_dir = Path(data_dir)
        self.settings = settings or get_settings()

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
        normalized_snapshot = self.get_normalized_snapshot(symbol, asset_class)
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
            "normalized": normalized_snapshot.to_dict(),
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
        snapshot = self.get_normalized_snapshot(symbol, asset_class or infer_asset_class(symbol))
        if snapshot.evaluation_price is None:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(snapshot.evaluation_price)

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        trade = self.get_latest_trade(symbol, resolved_asset_class)
        quote = self.get_latest_quote(symbol, resolved_asset_class)
        session = self.get_session_status(resolved_asset_class)
        fallback_price: float | None = None
        try:
            bars = self.get_bars(symbol, resolved_asset_class, timeframe="1D", limit=1)
            if bars:
                fallback_price = bars[-1].close
        except Exception:
            fallback_price = None
        return _build_normalized_snapshot(
            symbol=canonicalize_symbol(symbol, resolved_asset_class),
            asset_class=resolved_asset_class,
            session=session,
            trade=trade,
            quote=quote,
            quote_stale_after_seconds=self.settings.quote_stale_after_seconds,
            source="csv",
            fallback_price=fallback_price,
            exchange="MOCK",
        )

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        return {
            "symbol": canonicalize_symbol(symbol, AssetClass.OPTION),
            "expiration": expiration,
            "source": "csv",
            "status": "unsupported",
            "calls": [],
            "puts": [],
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": "csv",
            "data_dir": str(self.data_dir),
            "supported_symbols": self.list_supported_symbols(),
        }


class AlpacaMarketDataProvider(MarketDataProviderBase):
    provider_name = "alpaca"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: TTLCache | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        super().__init__(settings=settings, cache=cache, sleeper=sleeper)
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
        max_retries = max(0, int(self.settings.market_data_max_retries))
        for attempt in range(max_retries + 1):
            try:
                self.limiter.wait(path=path)
                response = self.client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    self._recent_429_count += 1
                if exc.response.status_code == 429 and attempt < max_retries:
                    retry_after = _retry_after_seconds(exc.response.headers.get("Retry-After"))
                    if retry_after is None:
                        base = float(self.settings.market_data_backoff_base_seconds)
                        cap = float(self.settings.market_data_backoff_max_seconds)
                        retry_after = min(cap, base * (2 ** attempt)) + random.uniform(0.0, base)
                    logger.warning(
                        "Alpaca market data 429; backing off",
                        extra={
                            "provider": self.provider_name,
                            "path": path,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "sleep_seconds": retry_after,
                        },
                    )
                    self._sleep(retry_after)
                    continue
                raise RuntimeError(
                    f"Alpaca market data error {exc.response.status_code}: {exc.response.text}"
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == max_retries:
                    raise RuntimeError(f"Alpaca market data request failed: {exc}") from exc
                base = float(self.settings.market_data_backoff_base_seconds)
                sleep_for = min(float(self.settings.market_data_backoff_max_seconds), base * (2 ** attempt))
                self._sleep(sleep_for)
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
        cache_key = self._cache_key("bars", resolved_symbol, resolved_asset_class.value, timeframe, limit, start, end)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
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
            bars = self._parse_crypto_bars(payload, resolved_symbol)
            self._set_cache(cache_key, bars, self._bars_cache_ttl(timeframe))
            return bars

        payload = self._request_json(
            f"/v2/stocks/{resolved_symbol}/bars",
            params={**params, "feed": "iex"},
        )
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(resolved_symbol)
        bars = self._parse_stock_bars(payload, resolved_symbol, resolved_asset_class)
        self._set_cache(cache_key, bars, self._bars_cache_ttl(timeframe))
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

    def _snapshot_payload_to_response(
        self,
        symbol: str,
        asset_class: AssetClass,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_symbol = canonicalize_symbol(symbol, asset_class)
        latest_trade = payload.get("latestTrade") or payload.get("latest_trade") or payload.get("trade") or {}
        latest_quote = payload.get("latestQuote") or payload.get("latest_quote") or payload.get("quote") or {}
        latest_bar = payload.get("latestBar") or payload.get("minuteBar") or payload.get("minute_bar") or {}
        daily_bar = payload.get("dailyBar") or payload.get("daily_bar") or {}
        previous_daily = payload.get("prevDailyBar") or payload.get("previousDailyBar") or payload.get("prev_daily_bar") or {}
        trade = self._parse_trade(latest_trade, resolved_symbol, asset_class)
        quote = self._parse_stock_quote(latest_quote, resolved_symbol, asset_class)
        fallback_price = _safe_float(latest_bar.get("c")) or _safe_float(daily_bar.get("c"))
        normalized = _build_normalized_snapshot(
            symbol=resolved_symbol,
            asset_class=asset_class,
            session=self.get_session_status(asset_class),
            trade=trade,
            quote=quote,
            quote_stale_after_seconds=self.settings.quote_stale_after_seconds,
            source=self.provider_name,
            fallback_price=fallback_price,
            exchange="IEX",
        )
        previous_close = _safe_float(previous_daily.get("c")) or _safe_float(daily_bar.get("o"))
        daily_change_pct = None
        if previous_close and previous_close > 0 and normalized.evaluation_price is not None:
            daily_change_pct = (normalized.evaluation_price - previous_close) / previous_close
        return {
            "symbol": resolved_symbol,
            "asset_class": asset_class.value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": self.get_session_status(asset_class).to_dict(),
            "daily_change_pct": daily_change_pct,
            "latest_bar": latest_bar,
            "daily_bar": daily_bar,
            "normalized": normalized.to_dict(),
            "source": self.provider_name,
        }

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("quote", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
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
            timestamp_value = (
                orderbook.get("t")
                or orderbook.get("timestamp")
                or best_ask.get("t")
                or best_bid.get("t")
            )
            quote = QuoteSnapshot(
                symbol=resolved_symbol,
                asset_class=AssetClass.CRYPTO,
                ask_price=float(best_ask["p"]) if best_ask.get("p") is not None else None,
                ask_size=float(best_ask["s"]) if best_ask.get("s") is not None else None,
                bid_price=float(best_bid["p"]) if best_bid.get("p") is not None else None,
                bid_size=float(best_bid["s"]) if best_bid.get("s") is not None else None,
                timestamp=_parse_timestamp(timestamp_value) if timestamp_value else datetime.now(timezone.utc),
            )
            self._set_cache(cache_key, quote, self.settings.snapshot_cache_ttl_seconds)
            return quote

        payload = self._request_json(f"/v2/stocks/{resolved_symbol}/quotes/latest", params={"feed": "iex"})
        quote = self._parse_stock_quote(payload, resolved_symbol, resolved_asset_class or infer_asset_class(resolved_symbol))
        self._set_cache(cache_key, quote, self.settings.snapshot_cache_ttl_seconds)
        return quote

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("trade", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        if resolved_asset_class == AssetClass.CRYPTO:
            payload = self._request_json(
                f"/v1beta3/crypto/{self.settings.alpaca_crypto_location}/latest/trades",
                params={"symbols": resolved_symbol},
            )
            trades = payload.get("trades") or {}
            trade = trades.get(resolved_symbol) or {}
            parsed_trade = self._parse_trade(trade, resolved_symbol, AssetClass.CRYPTO)
            self._set_cache(cache_key, parsed_trade, self.settings.snapshot_cache_ttl_seconds)
            return parsed_trade

        payload = self._request_json(f"/v2/stocks/{resolved_symbol}/trades/latest", params={"feed": "iex"})
        trade = self._parse_trade(payload, resolved_symbol, resolved_asset_class or infer_asset_class(resolved_symbol))
        self._set_cache(cache_key, trade, self.settings.snapshot_cache_ttl_seconds)
        return trade

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        resolved_asset_class = normalize_asset_class(asset_class)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("snapshot", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
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
        normalized_snapshot = self.get_normalized_snapshot(resolved_symbol, resolved_asset_class)
        prev_close = float(daily_bar.get("o", daily_bar.get("open", 0.0))) or None
        daily_change_pct = None
        if prev_close and prev_close > 0 and trade.price is not None:
            daily_change_pct = (trade.price - prev_close) / prev_close

        result = {
            "symbol": resolved_symbol,
            "asset_class": resolved_asset_class.value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": session.to_dict(),
            "daily_change_pct": daily_change_pct,
            "latest_bar": snapshot.get("latestBar") or snapshot.get("minuteBar") or snapshot.get("minute_bar"),
            "daily_bar": daily_bar,
            "normalized": normalized_snapshot.to_dict(),
        }
        self._set_cache(cache_key, result, self.settings.snapshot_cache_ttl_seconds)
        return result

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
        results: dict[str, dict[str, Any]] = {}
        for symbol, data in snapshots.items():
            canonical_symbol = canonicalize_symbol(symbol, resolved_asset_class)
            if not isinstance(data, dict):
                continue
            response = self._snapshot_payload_to_response(canonical_symbol, resolved_asset_class, data)
            results[canonical_symbol] = response
            self._set_cache(
                self._cache_key("snapshot", canonical_symbol, resolved_asset_class.value),
                response,
                self.settings.snapshot_cache_ttl_seconds,
            )
            self._set_cache(
                self._cache_key("normalized_snapshot", canonical_symbol, resolved_asset_class.value),
                NormalizedMarketSnapshot.from_dict(response["normalized"]),
                self.settings.snapshot_cache_ttl_seconds,
            )
        return results

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        return _session_status_for(resolved_asset_class)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        snapshot = self.get_normalized_snapshot(symbol, asset_class or infer_asset_class(symbol))
        if snapshot.evaluation_price is None:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(snapshot.evaluation_price)

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("normalized_snapshot", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        trade = self.get_latest_trade(resolved_symbol, resolved_asset_class)
        quote = self.get_latest_quote(resolved_symbol, resolved_asset_class)
        session = self.get_session_status(resolved_asset_class)
        fallback_price: float | None = None
        try:
            bars = self.get_bars(resolved_symbol, resolved_asset_class, timeframe=self.settings.default_timeframe, limit=1)
            if bars:
                fallback_price = bars[-1].close
        except Exception:
            fallback_price = None
        exchange = "CRYPTO" if resolved_asset_class == AssetClass.CRYPTO else "IEX"
        normalized = _build_normalized_snapshot(
            symbol=resolved_symbol,
            asset_class=resolved_asset_class,
            session=session,
            trade=trade,
            quote=quote,
            quote_stale_after_seconds=self.settings.quote_stale_after_seconds,
            source="alpaca",
            fallback_price=fallback_price,
            exchange=exchange,
        )
        self._set_cache(cache_key, normalized, self.settings.snapshot_cache_ttl_seconds)
        return normalized

    def close(self) -> None:
        self.client.close()


class YahooFinanceMarketDataProvider(MarketDataProviderBase):
    provider_name = "yfinance"

    def _load_yfinance(self) -> Any:
        try:
            import yfinance as yf  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("Yahoo Finance market data requires the optional 'yfinance' package.") from exc
        return yf

    def _provider_symbol(self, symbol: str, asset_class: AssetClass | str | None = None) -> str:
        resolved_asset_class = normalize_asset_class(asset_class)
        canonical_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        if resolved_asset_class == AssetClass.CRYPTO:
            return canonical_symbol.replace("/", "-")
        return canonical_symbol

    def _ticker(self, symbol: str, asset_class: AssetClass | str | None = None) -> Any:
        return self._load_yfinance().Ticker(self._provider_symbol(symbol, asset_class))

    def _download_batch(self, symbols: list[str], *, period: str, interval: str) -> pd.DataFrame:
        yf = self._load_yfinance()
        self.limiter.wait(path="yfinance.download")
        return yf.download(
            " ".join(symbols),
            period=period,
            interval=interval,
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

    def _interval_for_timeframe(self, timeframe: str | None) -> str:
        normalized = str(timeframe or "1D").strip().upper()
        mapping = {
            "1MIN": "1m",
            "1T": "1m",
            "5MIN": "5m",
            "5T": "5m",
            "15MIN": "15m",
            "15T": "15m",
            "30MIN": "30m",
            "30T": "30m",
            "1H": "1h",
            "1HOUR": "1h",
            "1D": "1d",
            "1DAY": "1d",
        }
        return mapping.get(normalized, "1d")

    def _period_for_history(self, timeframe: str | None, limit: int) -> str:
        delta = _timeframe_to_timedelta(timeframe or "1D")
        days = max(1, int((delta * max(limit, 2) * 2).total_seconds() // 86400) + 1)
        if delta < timedelta(days=1):
            return f"{min(max(days, 5), 60)}d"
        if days <= 30:
            return "1mo"
        if days <= 90:
            return "3mo"
        if days <= 180:
            return "6mo"
        if days <= 365:
            return "1y"
        if days <= 730:
            return "2y"
        return "5y"

    def _standardize_history_frame(self, frame: pd.DataFrame, *, limit: int | None = None) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        df = frame.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [str(col[-1] or col[0]) for col in df.columns]
        df = df.reset_index()
        rename_map: dict[str, str] = {}
        for column in df.columns:
            lowered = str(column).lower()
            if lowered in {"date", "datetime"}:
                rename_map[column] = "Date"
            elif lowered == "open":
                rename_map[column] = "Open"
            elif lowered == "high":
                rename_map[column] = "High"
            elif lowered == "low":
                rename_map[column] = "Low"
            elif lowered == "close":
                rename_map[column] = "Close"
            elif lowered == "volume":
                rename_map[column] = "Volume"
        df = df.rename(columns=rename_map)
        if "Date" not in df.columns:
            df["Date"] = pd.Timestamp.utcnow()
        for column in ["Open", "High", "Low", "Close", "Volume"]:
            if column not in df.columns:
                df[column] = 0.0
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if limit and len(df) > limit:
            df = df.tail(limit)
        return df.sort_values("Date").reset_index(drop=True)

    def _frame_for_symbol(self, downloaded: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if downloaded is None or downloaded.empty:
            return pd.DataFrame()
        if isinstance(downloaded.columns, pd.MultiIndex):
            level_zero = [str(item) for item in downloaded.columns.get_level_values(0)]
            if symbol in level_zero:
                return downloaded[symbol].dropna(how="all")
            level_one = [str(item) for item in downloaded.columns.get_level_values(1)]
            if symbol in level_one:
                return downloaded.xs(symbol, axis=1, level=1).dropna(how="all")
        return downloaded.dropna(how="all")

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("bars_df", resolved_symbol, resolved_asset_class.value, timeframe or "1D", limit)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached.copy()
        ticker = self._ticker(resolved_symbol, resolved_asset_class)
        period = self._period_for_history(timeframe, limit)
        interval = self._interval_for_timeframe(timeframe)
        self.limiter.wait(path="yfinance.history")
        raw = ticker.history(period=period, interval=interval, auto_adjust=False)
        df = self._standardize_history_frame(raw, limit=limit)
        if df.empty:
            raise RuntimeError(f"No Yahoo Finance bar data returned for {resolved_symbol}")
        self._set_cache(cache_key, df.copy(), self._bars_cache_ttl(timeframe))
        return df

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
        df = self.fetch_bars(symbol, timeframe=timeframe, limit=limit, asset_class=resolved_asset_class)
        if start is not None:
            df = df[df["Date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["Date"] <= pd.Timestamp(end)]
        return [
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
            for _, row in df.iterrows()
        ]

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("trade", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        ticker = self._ticker(resolved_symbol, resolved_asset_class)
        price = None
        try:
            self.limiter.wait(path="yfinance.fast_info")
            fast_info = getattr(ticker, "fast_info", {}) or {}
            getter = fast_info.get if hasattr(fast_info, "get") else lambda key, default=None: getattr(fast_info, key, default)
            price = _safe_float(getter("last_price")) or _safe_float(getter("lastPrice"))
        except Exception:
            price = None
        timestamp = datetime.now(timezone.utc)
        volume = None
        if price is None:
            df = self.fetch_bars(resolved_symbol, timeframe="1D", limit=2, asset_class=resolved_asset_class)
            latest = df.iloc[-1]
            price = float(latest["Close"])
            timestamp = _parse_timestamp(latest["Date"])
            volume = _safe_float(latest.get("Volume"))
        trade = TradeSnapshot(
            symbol=resolved_symbol,
            asset_class=resolved_asset_class,
            price=price,
            size=volume,
            timestamp=timestamp,
        )
        self._set_cache(cache_key, trade, self.settings.snapshot_cache_ttl_seconds)
        return trade

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("quote", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        bid_price = None
        ask_price = None
        bid_size = None
        ask_size = None
        try:
            ticker = self._ticker(resolved_symbol, resolved_asset_class)
            self.limiter.wait(path="yfinance.info")
            info = getattr(ticker, "info", {}) or {}
            bid_price = _safe_float(info.get("bid"))
            ask_price = _safe_float(info.get("ask"))
            bid_size = _safe_float(info.get("bidSize"))
            ask_size = _safe_float(info.get("askSize"))
        except Exception as exc:
            logger.debug(
                "Yahoo Finance quote fields unavailable",
                extra={"provider": self.provider_name, "symbol": resolved_symbol, "error": str(exc)},
            )
        quote = QuoteSnapshot(
            symbol=resolved_symbol,
            asset_class=resolved_asset_class,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            timestamp=datetime.now(timezone.utc),
        )
        self._set_cache(cache_key, quote, self.settings.snapshot_cache_ttl_seconds)
        return quote

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("snapshot", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        trade = self.get_latest_trade(resolved_symbol, resolved_asset_class)
        quote = self.get_latest_quote(resolved_symbol, resolved_asset_class)
        bars = self.get_bars(resolved_symbol, resolved_asset_class, timeframe="1D", limit=2)
        previous_close = bars[-2].close if len(bars) > 1 else None
        daily_change_pct = None
        if previous_close and previous_close > 0 and trade.price is not None:
            daily_change_pct = (trade.price - previous_close) / previous_close
        normalized = self.get_normalized_snapshot(resolved_symbol, resolved_asset_class)
        result = {
            "symbol": resolved_symbol,
            "asset_class": resolved_asset_class.value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": self.get_session_status(resolved_asset_class).to_dict(),
            "daily_change_pct": daily_change_pct,
            "latest_bar": bars[-1].to_dict() if bars else None,
            "normalized": normalized.to_dict(),
            "source": self.provider_name,
            "partial": {
                "quote_fields_missing": not normalized.quote_available,
                "volume_missing": not bars or bars[-1].volume <= 0,
            },
        }
        self._set_cache(cache_key, result, self.settings.snapshot_cache_ttl_seconds)
        return result

    def batch_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass | str,
    ) -> dict[str, dict[str, Any]]:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        canonical_symbols = [canonicalize_symbol(symbol, resolved_asset_class) for symbol in symbols]
        if not canonical_symbols:
            return {}
        provider_symbols = [self._provider_symbol(symbol, resolved_asset_class) for symbol in canonical_symbols]
        try:
            downloaded = self._download_batch(provider_symbols, period="5d", interval="1d")
        except Exception as exc:
            logger.warning(
                "Yahoo Finance batch snapshot failed; falling back to per-symbol snapshots",
                extra={"provider": self.provider_name, "asset_class": resolved_asset_class.value, "error": str(exc)},
            )
            return {
                canonicalize_symbol(symbol, resolved_asset_class): self.get_snapshot(symbol, resolved_asset_class)
                for symbol in canonical_symbols
            }

        results: dict[str, dict[str, Any]] = {}
        for canonical_symbol, provider_symbol in zip(canonical_symbols, provider_symbols, strict=False):
            frame = self._standardize_history_frame(self._frame_for_symbol(downloaded, provider_symbol), limit=2)
            if frame.empty:
                logger.warning(
                    "Yahoo Finance batch snapshot missing symbol data",
                    extra={"provider": self.provider_name, "symbol": canonical_symbol},
                )
                continue
            latest = frame.iloc[-1]
            previous = frame.iloc[-2] if len(frame) > 1 else latest
            price = _safe_float(latest["Close"])
            timestamp = _parse_timestamp(latest["Date"])
            volume = _safe_float(latest.get("Volume"))
            normalized = self._normalized_from_price(
                symbol=canonical_symbol,
                asset_class=resolved_asset_class,
                price=price,
                timestamp=timestamp,
                source=self.provider_name,
                exchange="YFINANCE",
                volume=volume,
            )
            previous_close = _safe_float(previous.get("Close"))
            daily_change_pct = None
            if previous_close and previous_close > 0 and price is not None:
                daily_change_pct = (price - previous_close) / previous_close
            result = {
                "symbol": canonical_symbol,
                "asset_class": resolved_asset_class.value,
                "quote": QuoteSnapshot(
                    symbol=canonical_symbol,
                    asset_class=resolved_asset_class,
                    timestamp=timestamp,
                ).to_dict(),
                "trade": TradeSnapshot(
                    symbol=canonical_symbol,
                    asset_class=resolved_asset_class,
                    price=price,
                    size=volume,
                    timestamp=timestamp,
                ).to_dict(),
                "session": self.get_session_status(resolved_asset_class).to_dict(),
                "daily_change_pct": daily_change_pct,
                "latest_bar": NormalizedBar(
                    symbol=canonical_symbol,
                    asset_class=resolved_asset_class,
                    timestamp=timestamp,
                    open=float(latest["Open"]),
                    high=float(latest["High"]),
                    low=float(latest["Low"]),
                    close=float(latest["Close"]),
                    volume=float(latest["Volume"]),
                ).to_dict(),
                "normalized": normalized.to_dict(),
                "source": self.provider_name,
                "partial": {"quote_fields_missing": True},
            }
            results[canonical_symbol] = result
            self._set_cache(
                self._cache_key("snapshot", canonical_symbol, resolved_asset_class.value),
                result,
                self.settings.snapshot_cache_ttl_seconds,
            )
            self._set_cache(
                self._cache_key("normalized_snapshot", canonical_symbol, resolved_asset_class.value),
                normalized,
                self.settings.snapshot_cache_ttl_seconds,
            )
        return results

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        return _session_status_for(resolved_asset_class)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        snapshot = self.get_normalized_snapshot(symbol, asset_class or infer_asset_class(symbol))
        if snapshot.evaluation_price is None:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(snapshot.evaluation_price)

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        resolved_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        cache_key = self._cache_key("normalized_snapshot", resolved_symbol, resolved_asset_class.value)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        trade = self.get_latest_trade(resolved_symbol, resolved_asset_class)
        quote = self.get_latest_quote(resolved_symbol, resolved_asset_class)
        fallback_price = trade.price
        if fallback_price is None:
            try:
                bars = self.get_bars(resolved_symbol, resolved_asset_class, timeframe="1D", limit=1)
                fallback_price = bars[-1].close if bars else None
            except Exception:
                fallback_price = None
        normalized = _build_normalized_snapshot(
            symbol=resolved_symbol,
            asset_class=resolved_asset_class,
            session=self.get_session_status(resolved_asset_class),
            trade=trade,
            quote=quote,
            quote_stale_after_seconds=self.settings.quote_stale_after_seconds,
            source=self.provider_name,
            fallback_price=fallback_price,
            exchange="YFINANCE",
        )
        if not normalized.quote_available:
            logger.debug(
                "Yahoo Finance snapshot is partial",
                extra={"provider": self.provider_name, "symbol": resolved_symbol, "missing": ["bid", "ask"]},
            )
        self._set_cache(cache_key, normalized, self.settings.snapshot_cache_ttl_seconds)
        return normalized

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        underlying = canonicalize_symbol(symbol, AssetClass.EQUITY)
        cache_key = self._cache_key("option_chain", underlying, expiration or "")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        ticker = self._ticker(underlying, AssetClass.EQUITY)
        self.limiter.wait(path="yfinance.options")
        expirations = list(getattr(ticker, "options", []) or [])
        selected_expiration = expiration or (expirations[0] if expirations else None)
        if not selected_expiration:
            result = {
                "symbol": underlying,
                "expiration": expiration,
                "expirations": [],
                "source": self.provider_name,
                "status": "no_expirations",
                "calls": [],
                "puts": [],
            }
            self._set_cache(cache_key, result, self.settings.option_chain_cache_ttl_seconds)
            return result
        self.limiter.wait(path="yfinance.option_chain")
        chain = ticker.option_chain(selected_expiration)
        calls = getattr(chain, "calls", pd.DataFrame())
        puts = getattr(chain, "puts", pd.DataFrame())
        result = {
            "symbol": underlying,
            "expiration": selected_expiration,
            "expirations": expirations,
            "source": self.provider_name,
            "status": "ok",
            "calls": calls.where(pd.notnull(calls), None).to_dict(orient="records"),
            "puts": puts.where(pd.notnull(puts), None).to_dict(orient="records"),
        }
        self._set_cache(cache_key, result, self.settings.option_chain_cache_ttl_seconds)
        return result


COINGECKO_SYMBOL_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "LINK": "chainlink",
    "LTC": "litecoin",
    "ADA": "cardano",
    "XRP": "ripple",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "USDC": "usd-coin",
}


def coingecko_coin_id_for_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper().replace("-", "/")
    if "/" in normalized:
        base = normalized.split("/", 1)[0]
    else:
        base = normalized
        for suffix in ("USDT", "USDC", "USD"):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
    coin_id = COINGECKO_SYMBOL_MAP.get(base.upper())
    if not coin_id:
        raise ValueError(f"No CoinGecko symbol mapping configured for {symbol}.")
    return coin_id


class CoinGeckoMarketDataProvider(MarketDataProviderBase):
    provider_name = "coingecko"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: TTLCache | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        super().__init__(settings=settings, cache=cache, sleeper=sleeper)
        headers = {"accept": "application/json"}
        if self.settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self.settings.coingecko_api_key
        self.client = httpx.Client(
            base_url=str(self.settings.coingecko_base_url).rstrip("/"),
            headers=headers,
            timeout=10.0,
        )

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            self.limiter.wait(path=path)
            response = self.client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                self._recent_429_count += 1
            raise RuntimeError(f"CoinGecko market data error {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"CoinGecko market data request failed: {exc}") from exc

    def _coin_id(self, symbol: str) -> str:
        return coingecko_coin_id_for_symbol(symbol)

    def _ohlc_days(self, timeframe: str | None, limit: int) -> int:
        delta = _timeframe_to_timedelta(timeframe or "1D")
        requested_days = max(1, int((delta * max(limit, 2) * 1.5).total_seconds() // 86400) + 1)
        for candidate in [1, 7, 14, 30, 90, 180, 365]:
            if requested_days <= candidate:
                return candidate
        return 365

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        coin_id = self._coin_id(resolved_symbol)
        cache_key = self._cache_key("bars_df", resolved_symbol, "crypto", timeframe or "1D", limit)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached.copy()
        payload = self._request_json(
            f"/api/v3/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": self._ohlc_days(timeframe, limit)},
        )
        rows = []
        for item in payload or []:
            if len(item) < 5:
                continue
            rows.append(
                {
                    "Date": pd.to_datetime(int(item[0]), unit="ms", utc=True),
                    "Open": float(item[1]),
                    "High": float(item[2]),
                    "Low": float(item[3]),
                    "Close": float(item[4]),
                    "Volume": 0.0,
                }
            )
        df = pd.DataFrame(rows)
        if df.empty:
            raise RuntimeError(f"No CoinGecko OHLC data returned for {resolved_symbol}")
        if len(df) > limit:
            df = df.tail(limit)
        df = df.sort_values("Date").reset_index(drop=True)
        self._set_cache(cache_key, df.copy(), self._bars_cache_ttl(timeframe))
        return df

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass | str,
        timeframe: str,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[NormalizedBar]:
        df = self.fetch_bars(symbol, timeframe=timeframe, limit=limit, asset_class=AssetClass.CRYPTO)
        if start is not None:
            df = df[df["Date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["Date"] <= pd.Timestamp(end)]
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        return [
            NormalizedBar(
                symbol=resolved_symbol,
                asset_class=AssetClass.CRYPTO,
                timestamp=_parse_timestamp(row["Date"]),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
            for _, row in df.iterrows()
        ]

    def _simple_price(self, symbols: list[str]) -> dict[str, Any]:
        canonical_symbols = [canonicalize_symbol(symbol, AssetClass.CRYPTO) for symbol in symbols]
        coin_ids = [self._coin_id(symbol) for symbol in canonical_symbols]
        payload = self._request_json(
            "/api/v3/simple/price",
            params={
                "ids": ",".join(coin_ids),
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
        )
        return payload if isinstance(payload, dict) else {}

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        cache_key = self._cache_key("trade", resolved_symbol, "crypto")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        coin_id = self._coin_id(resolved_symbol)
        data = self._simple_price([resolved_symbol]).get(coin_id) or {}
        timestamp = datetime.fromtimestamp(float(data.get("last_updated_at")), tz=timezone.utc) if data.get("last_updated_at") else datetime.now(timezone.utc)
        trade = TradeSnapshot(
            symbol=resolved_symbol,
            asset_class=AssetClass.CRYPTO,
            price=_safe_float(data.get("usd")),
            size=_safe_float(data.get("usd_24h_vol")),
            timestamp=timestamp,
        )
        self._set_cache(cache_key, trade, self.settings.snapshot_cache_ttl_seconds)
        return trade

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        cache_key = self._cache_key("quote", resolved_symbol, "crypto")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        quote = QuoteSnapshot(
            symbol=resolved_symbol,
            asset_class=AssetClass.CRYPTO,
            timestamp=datetime.now(timezone.utc),
        )
        self._set_cache(cache_key, quote, self.settings.snapshot_cache_ttl_seconds)
        return quote

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        cache_key = self._cache_key("snapshot", resolved_symbol, "crypto")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        coin_id = self._coin_id(resolved_symbol)
        data = self._simple_price([resolved_symbol]).get(coin_id) or {}
        price = _safe_float(data.get("usd"))
        timestamp = datetime.fromtimestamp(float(data.get("last_updated_at")), tz=timezone.utc) if data.get("last_updated_at") else datetime.now(timezone.utc)
        trade = TradeSnapshot(
            symbol=resolved_symbol,
            asset_class=AssetClass.CRYPTO,
            price=price,
            size=_safe_float(data.get("usd_24h_vol")),
            timestamp=timestamp,
        )
        quote = QuoteSnapshot(symbol=resolved_symbol, asset_class=AssetClass.CRYPTO, timestamp=timestamp)
        normalized = self._normalized_from_price(
            symbol=resolved_symbol,
            asset_class=AssetClass.CRYPTO,
            price=price,
            timestamp=timestamp,
            source=self.provider_name,
            exchange="COINGECKO",
            volume=_safe_float(data.get("usd_24h_vol")),
        )
        result = {
            "symbol": resolved_symbol,
            "asset_class": AssetClass.CRYPTO.value,
            "quote": quote.to_dict(),
            "trade": trade.to_dict(),
            "session": self.get_session_status(AssetClass.CRYPTO).to_dict(),
            "daily_change_pct": (_safe_float(data.get("usd_24h_change")) or 0.0) / 100.0 if data.get("usd_24h_change") is not None else None,
            "latest_bar": None,
            "normalized": normalized.to_dict(),
            "source": self.provider_name,
            "partial": {"quote_fields_missing": True, "volume_is_24h_quote_volume": True},
        }
        self._set_cache(cache_key, result, self.settings.snapshot_cache_ttl_seconds)
        self._set_cache(
            self._cache_key("normalized_snapshot", resolved_symbol, "crypto"),
            normalized,
            self.settings.snapshot_cache_ttl_seconds,
        )
        return result

    def batch_snapshot(self, symbols: list[str], asset_class: AssetClass | str) -> dict[str, dict[str, Any]]:
        canonical_symbols: list[str] = []
        coin_ids: list[str] = []
        for symbol in symbols:
            canonical_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
            try:
                coin_id = self._coin_id(canonical_symbol)
            except ValueError as exc:
                logger.warning(
                    "CoinGecko symbol mapping missing",
                    extra={"provider": self.provider_name, "symbol": canonical_symbol, "error": str(exc)},
                )
                continue
            canonical_symbols.append(canonical_symbol)
            coin_ids.append(coin_id)
        if not canonical_symbols:
            return {}
        payload = self._request_json(
            "/api/v3/simple/price",
            params={
                "ids": ",".join(coin_ids),
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
        )
        results: dict[str, dict[str, Any]] = {}
        for canonical_symbol, coin_id in zip(canonical_symbols, coin_ids, strict=False):
            data = (payload or {}).get(coin_id) or {}
            timestamp = datetime.fromtimestamp(float(data.get("last_updated_at")), tz=timezone.utc) if data.get("last_updated_at") else datetime.now(timezone.utc)
            normalized = self._normalized_from_price(
                symbol=canonical_symbol,
                asset_class=AssetClass.CRYPTO,
                price=_safe_float(data.get("usd")),
                timestamp=timestamp,
                source=self.provider_name,
                exchange="COINGECKO",
                volume=_safe_float(data.get("usd_24h_vol")),
            )
            result = {
                "symbol": canonical_symbol,
                "asset_class": AssetClass.CRYPTO.value,
                "quote": QuoteSnapshot(
                    symbol=canonical_symbol,
                    asset_class=AssetClass.CRYPTO,
                    timestamp=timestamp,
                ).to_dict(),
                "trade": TradeSnapshot(
                    symbol=canonical_symbol,
                    asset_class=AssetClass.CRYPTO,
                    price=_safe_float(data.get("usd")),
                    size=_safe_float(data.get("usd_24h_vol")),
                    timestamp=timestamp,
                ).to_dict(),
                "session": self.get_session_status(AssetClass.CRYPTO).to_dict(),
                "daily_change_pct": (_safe_float(data.get("usd_24h_change")) or 0.0) / 100.0 if data.get("usd_24h_change") is not None else None,
                "latest_bar": None,
                "normalized": normalized.to_dict(),
                "source": self.provider_name,
                "partial": {"quote_fields_missing": True, "volume_is_24h_quote_volume": True},
            }
            results[canonical_symbol] = result
            self._set_cache(self._cache_key("snapshot", canonical_symbol, "crypto"), result, self.settings.snapshot_cache_ttl_seconds)
            self._set_cache(self._cache_key("normalized_snapshot", canonical_symbol, "crypto"), normalized, self.settings.snapshot_cache_ttl_seconds)
        return results

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        return _session_status_for(AssetClass.CRYPTO)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        snapshot = self.get_normalized_snapshot(symbol, AssetClass.CRYPTO)
        if snapshot.evaluation_price is None:
            raise RuntimeError(f"No latest price available for symbol {symbol}")
        return float(snapshot.evaluation_price)

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
        resolved_symbol = canonicalize_symbol(symbol, AssetClass.CRYPTO)
        cache_key = self._cache_key("normalized_snapshot", resolved_symbol, "crypto")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        return NormalizedMarketSnapshot.from_dict(self.get_snapshot(resolved_symbol, AssetClass.CRYPTO)["normalized"])

    def close(self) -> None:
        self.client.close()


class TradierOptionsMarketDataProvider(MarketDataProviderBase):
    provider_name = "tradier"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: TTLCache | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        super().__init__(settings=settings, cache=cache, sleeper=sleeper)
        if not self.settings.tradier_api_token:
            raise ValueError("Tradier options data requires TRADIER_API_TOKEN.")
        self.client = httpx.Client(
            base_url=str(self.settings.tradier_base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {self.settings.tradier_api_token}", "Accept": "application/json"},
            timeout=10.0,
        )

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        underlying = canonicalize_symbol(symbol, AssetClass.EQUITY)
        cache_key = self._cache_key("option_chain", underlying, expiration or "")
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        params = {"symbol": underlying, "greeks": "false"}
        if expiration:
            params["expiration"] = expiration
        try:
            self.limiter.wait(path="/v1/markets/options/chains")
            response = self.client.get("/v1/markets/options/chains", params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                self._recent_429_count += 1
            raise RuntimeError(f"Tradier options data error {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Tradier options data request failed: {exc}") from exc
        options = ((payload or {}).get("options") or {}).get("option") or []
        if isinstance(options, dict):
            options = [options]
        result = {
            "symbol": underlying,
            "expiration": expiration,
            "source": self.provider_name,
            "status": "ok",
            "options": options,
            "calls": [item for item in options if str(item.get("option_type", "")).lower() == "call"],
            "puts": [item for item in options if str(item.get("option_type", "")).lower() == "put"],
        }
        self._set_cache(cache_key, result, self.settings.option_chain_cache_ttl_seconds)
        return result

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        return _session_status_for(AssetClass.OPTION)

    def close(self) -> None:
        self.client.close()


DEFAULT_PROVIDER_BY_ASSET_CLASS: dict[AssetClass, str] = {
    AssetClass.EQUITY: "yfinance",
    AssetClass.ETF: "yfinance",
    AssetClass.CRYPTO: "coingecko",
    AssetClass.OPTION: "yfinance",
}


class CompositeMarketDataProvider:
    provider_name = "composite"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers: dict[str, MarketDataService] | None = None,
        route_by_asset_class: dict[AssetClass, str] | None = None,
        fallback_provider_names: list[str] | None = None,
    ):
        self.settings = settings or get_settings()
        self.providers: dict[str, MarketDataService] = providers or self._build_default_providers()
        self.route_by_asset_class = route_by_asset_class or self._build_routes()
        self.fallback_provider_names = fallback_provider_names if fallback_provider_names is not None else list(self.settings.market_data_fallback_providers)
        self._recent_fallback_count = 0
        logger.info(
            "Market data providers selected",
            extra={
                "provider": self.provider_name,
                "routes": {asset_class.value: provider for asset_class, provider in self.route_by_asset_class.items()},
                "fallbacks": self.fallback_provider_names,
            },
        )

    def _build_default_providers(self) -> dict[str, MarketDataService]:
        providers: dict[str, MarketDataService] = {
            "yfinance": YahooFinanceMarketDataProvider(self.settings),
            "coingecko": CoinGeckoMarketDataProvider(self.settings),
        }
        if self.settings.has_alpaca_credentials:
            providers["alpaca"] = AlpacaMarketDataProvider(self.settings)
        if self.settings.tradier_api_token:
            providers["tradier"] = TradierOptionsMarketDataProvider(self.settings)
        return providers

    def _build_routes(self) -> dict[AssetClass, str]:
        routes = {
            AssetClass.EQUITY: self.settings.equity_data_provider,
            AssetClass.ETF: self.settings.etf_data_provider,
            AssetClass.CRYPTO: self.settings.crypto_data_provider,
            AssetClass.OPTION: self.settings.option_data_provider,
        }
        normalized: dict[AssetClass, str] = {}
        for asset_class, provider_name in routes.items():
            candidate = str(provider_name or "").strip().lower()
            if not candidate or candidate == "composite":
                candidate = DEFAULT_PROVIDER_BY_ASSET_CLASS[asset_class]
            normalized[asset_class] = candidate
        return normalized

    def _provider_name_for_asset_class(self, asset_class: AssetClass | str | None) -> str:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = AssetClass.EQUITY
        return self.route_by_asset_class.get(resolved_asset_class, DEFAULT_PROVIDER_BY_ASSET_CLASS.get(resolved_asset_class, "yfinance"))

    def _provider_for_asset_class(self, asset_class: AssetClass | str | None) -> MarketDataService:
        provider_name = self._provider_name_for_asset_class(asset_class)
        provider = self.providers.get(provider_name)
        if provider is None:
            raise RuntimeError(f"Market data provider '{provider_name}' is not configured.")
        return provider

    def _fallback_candidates(self, primary_name: str) -> list[tuple[str, MarketDataService]]:
        candidates: list[tuple[str, MarketDataService]] = []
        for provider_name in self.fallback_provider_names:
            normalized = str(provider_name).strip().lower()
            if not normalized or normalized == primary_name:
                continue
            provider = self.providers.get(normalized)
            if provider is not None:
                candidates.append((normalized, provider))
        return candidates

    def _call(
        self,
        method_name: str,
        asset_class: AssetClass | str | None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        primary_name = self._provider_name_for_asset_class(asset_class)
        primary = self._provider_for_asset_class(asset_class)
        try:
            return getattr(primary, method_name)(*args, **kwargs)
        except Exception as primary_exc:
            for fallback_name, fallback in self._fallback_candidates(primary_name):
                try:
                    self._recent_fallback_count += 1
                    logger.warning(
                        "Market data provider fallback",
                        extra={
                            "provider": primary_name,
                            "fallback_provider": fallback_name,
                            "method": method_name,
                            "asset_class": normalize_asset_class(asset_class).value,
                            "error": str(primary_exc),
                        },
                    )
                    return getattr(fallback, method_name)(*args, **kwargs)
                except Exception as fallback_exc:
                    logger.warning(
                        "Market data fallback provider failed",
                        extra={
                            "provider": fallback_name,
                            "method": method_name,
                            "asset_class": normalize_asset_class(asset_class).value,
                            "error": str(fallback_exc),
                        },
                    )
            raise

    def get_bars(
        self,
        symbol: str,
        asset_class: AssetClass | str,
        timeframe: str,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[NormalizedBar]:
        return self._call("get_bars", asset_class, symbol, asset_class, timeframe, limit, start=start, end=end)

    def get_latest_quote(self, symbol: str, asset_class: AssetClass | str) -> QuoteSnapshot:
        return self._call("get_latest_quote", asset_class, symbol, asset_class)

    def get_latest_trade(self, symbol: str, asset_class: AssetClass | str) -> TradeSnapshot:
        return self._call("get_latest_trade", asset_class, symbol, asset_class)

    def get_snapshot(self, symbol: str, asset_class: AssetClass | str) -> dict[str, Any]:
        return self._call("get_snapshot", asset_class, symbol, asset_class)

    def get_normalized_snapshot(
        self,
        symbol: str,
        asset_class: AssetClass | str,
    ) -> NormalizedMarketSnapshot:
        return self._call("get_normalized_snapshot", asset_class, symbol, asset_class)

    def batch_snapshot(
        self,
        symbols: list[str],
        asset_class: AssetClass | str,
    ) -> dict[str, dict[str, Any]]:
        resolved_asset_class = normalize_asset_class(asset_class)
        primary_name = self._provider_name_for_asset_class(resolved_asset_class)
        primary = self._provider_for_asset_class(resolved_asset_class)
        try:
            results = primary.batch_snapshot(symbols, resolved_asset_class)
        except Exception as exc:
            results = {}
            primary_error: Exception | None = exc
        else:
            primary_error = None

        missing_symbols = [
            canonicalize_symbol(symbol, resolved_asset_class)
            for symbol in symbols
            if canonicalize_symbol(symbol, resolved_asset_class) not in results
        ]
        if not missing_symbols and primary_error is None:
            return results

        for fallback_name, fallback in self._fallback_candidates(primary_name):
            try:
                self._recent_fallback_count += 1
                logger.warning(
                    "Market data batch fallback",
                    extra={
                        "provider": primary_name,
                        "fallback_provider": fallback_name,
                        "asset_class": resolved_asset_class.value,
                        "missing_symbol_count": len(missing_symbols),
                        "error": str(primary_error) if primary_error else None,
                    },
                )
                fallback_results = fallback.batch_snapshot(missing_symbols or symbols, resolved_asset_class)
                results.update(fallback_results)
                missing_symbols = [symbol for symbol in missing_symbols if symbol not in fallback_results]
                if not missing_symbols:
                    return results
            except Exception as fallback_exc:
                logger.warning(
                    "Market data batch fallback provider failed",
                    extra={
                        "provider": fallback_name,
                        "asset_class": resolved_asset_class.value,
                        "error": str(fallback_exc),
                    },
                )
        if primary_error is not None and not results:
            raise primary_error
        return results

    def get_session_status(self, asset_class: AssetClass | str) -> MarketSessionStatus:
        return self._call("get_session_status", asset_class, asset_class)

    def fetch_bars(
        self,
        symbol: str,
        timeframe: str | None = None,
        limit: int = 50,
        asset_class: AssetClass | str | None = None,
    ) -> pd.DataFrame:
        resolved_asset_class = asset_class or infer_asset_class(symbol)
        return self._call("fetch_bars", resolved_asset_class, symbol, timeframe=timeframe, limit=limit, asset_class=resolved_asset_class)

    def get_latest_price(self, symbol: str, asset_class: AssetClass | str | None = None) -> float:
        resolved_asset_class = asset_class or infer_asset_class(symbol)
        return self._call("get_latest_price", resolved_asset_class, symbol, resolved_asset_class)

    def get_option_chain(self, symbol: str, expiration: str | None = None) -> dict[str, Any]:
        asset_class = AssetClass.OPTION
        provider_name = self._provider_name_for_asset_class(asset_class)
        provider = self._provider_for_asset_class(asset_class)
        try:
            return provider.get_option_chain(symbol, expiration)
        except Exception as primary_exc:
            for fallback_name, fallback in self._fallback_candidates(provider_name):
                try:
                    return fallback.get_option_chain(symbol, expiration)
                except Exception:
                    continue
            raise primary_exc

    def diagnostics(self) -> dict[str, Any]:
        provider_diagnostics = {}
        recent_429_count = 0
        for name, provider in self.providers.items():
            diagnostics = provider.diagnostics() if hasattr(provider, "diagnostics") else {"provider": name}
            provider_diagnostics[name] = diagnostics
            recent_429_count += int(diagnostics.get("recent_429_count", 0) or 0)
        return {
            "provider": self.provider_name,
            "active_provider_by_asset_class": {
                asset_class.value: provider for asset_class, provider in self.route_by_asset_class.items()
            },
            "fallback_providers": list(self.fallback_provider_names),
            "providers": provider_diagnostics,
            "recent_429_count": recent_429_count,
            "recent_fallback_count": self._recent_fallback_count,
        }

    def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()


class AlpacaMarketDataService(AlpacaMarketDataProvider):
    """Backward-compatible name for the Alpaca market-data provider."""


def create_market_data_service(settings: Settings | None = None) -> MarketDataService:
    settings = settings or get_settings()
    provider_name = str(settings.market_data_provider_default or "composite").strip().lower()
    if settings.is_mock_mode and provider_name in {"composite", "mock", "csv"}:
        return CSVMarketDataService(settings=settings)
    if provider_name in {"mock", "csv"}:
        return CSVMarketDataService(settings=settings)
    if provider_name == "alpaca":
        return AlpacaMarketDataProvider(settings)
    if provider_name == "yfinance":
        return YahooFinanceMarketDataProvider(settings)
    if provider_name == "coingecko":
        return CoinGeckoMarketDataProvider(settings)
    return CompositeMarketDataProvider(settings)
