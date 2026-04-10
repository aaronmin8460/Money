from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    OPTION = "option"
    UNKNOWN = "unknown"


class SessionState(str, Enum):
    PREMARKET = "premarket"
    REGULAR = "regular"
    POSTMARKET = "postmarket"
    CLOSED = "closed"
    ALWAYS_OPEN = "always_open"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(slots=True)
class AssetMetadata:
    symbol: str
    name: str
    asset_class: AssetClass
    exchange: str | None = None
    status: str = "active"
    tradable: bool = True
    fractionable: bool = False
    shortable: bool = False
    easy_to_borrow: bool = False
    marginable: bool = False
    attributes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "asset_class": self.asset_class.value,
            "exchange": self.exchange,
            "status": self.status,
            "tradable": self.tradable,
            "fractionable": self.fractionable,
            "shortable": self.shortable,
            "easy_to_borrow": self.easy_to_borrow,
            "marginable": self.marginable,
            "attributes": list(self.attributes),
            "raw": self.raw,
        }


@dataclass(slots=True)
class NormalizedBar:
    symbol: str
    asset_class: AssetClass
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "trade_count": self.trade_count,
            "vwap": self.vwap,
        }


@dataclass(slots=True)
class QuoteSnapshot:
    symbol: str
    asset_class: AssetClass
    ask_price: float | None = None
    ask_size: float | None = None
    bid_price: float | None = None
    bid_size: float | None = None
    timestamp: datetime | None = None

    @property
    def spread(self) -> float | None:
        if self.ask_price is None or self.bid_price is None:
            return None
        return max(0.0, self.ask_price - self.bid_price)

    @property
    def spread_pct(self) -> float | None:
        if self.spread is None or self.bid_price is None or self.bid_price <= 0:
            return None
        mid = ((self.ask_price or 0.0) + (self.bid_price or 0.0)) / 2
        if mid <= 0:
            return None
        return self.spread / mid

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "ask_price": self.ask_price,
            "ask_size": self.ask_size,
            "bid_price": self.bid_price,
            "bid_size": self.bid_size,
            "spread": self.spread,
            "spread_pct": self.spread_pct,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(slots=True)
class TradeSnapshot:
    symbol: str
    asset_class: AssetClass
    price: float | None = None
    size: float | None = None
    timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "price": self.price,
            "size": self.size,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(slots=True)
class MarketSessionStatus:
    asset_class: AssetClass
    is_open: bool
    session_state: SessionState
    extended_hours: bool
    is_24_7: bool
    next_open: datetime | None = None
    next_close: datetime | None = None
    as_of: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class.value,
            "is_open": self.is_open,
            "session_state": self.session_state.value,
            "extended_hours": self.extended_hours,
            "is_24_7": self.is_24_7,
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "next_close": self.next_close.isoformat() if self.next_close else None,
            "as_of": self.as_of.isoformat(),
        }


@dataclass(slots=True)
class NormalizedMarketSnapshot:
    symbol: str
    asset_class: AssetClass
    last_trade_price: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    mid_price: float | None = None
    spread_abs: float | None = None
    spread_pct: float | None = None
    quote_available: bool = False
    quote_stale: bool = False
    quote_timestamp: datetime | None = None
    trade_timestamp: datetime | None = None
    source_timestamp: datetime | None = None
    quote_age_seconds: float | None = None
    fallback_pricing_used: bool = False
    price_source_used: str = "last_trade"
    evaluation_price: float | None = None
    session_state: str | None = None
    exchange: str | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NormalizedMarketSnapshot":
        def _parse_ts(value: Any) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value
            text = str(value).replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return None

        asset_class_value = payload.get("asset_class", AssetClass.UNKNOWN.value)
        try:
            resolved_asset_class = AssetClass(str(asset_class_value))
        except ValueError:
            resolved_asset_class = AssetClass.UNKNOWN
        return cls(
            symbol=str(payload.get("symbol", "")),
            asset_class=resolved_asset_class,
            last_trade_price=payload.get("last_trade_price"),
            bid_price=payload.get("bid_price"),
            ask_price=payload.get("ask_price"),
            mid_price=payload.get("mid_price"),
            spread_abs=payload.get("spread_abs"),
            spread_pct=payload.get("spread_pct"),
            quote_available=bool(payload.get("quote_available", False)),
            quote_stale=bool(payload.get("quote_stale", False)),
            quote_timestamp=_parse_ts(payload.get("quote_timestamp")),
            trade_timestamp=_parse_ts(payload.get("trade_timestamp")),
            source_timestamp=_parse_ts(payload.get("source_timestamp")),
            quote_age_seconds=payload.get("quote_age_seconds"),
            fallback_pricing_used=bool(payload.get("fallback_pricing_used", False)),
            price_source_used=str(payload.get("price_source_used", "last_trade")),
            evaluation_price=payload.get("evaluation_price"),
            session_state=payload.get("session_state"),
            exchange=payload.get("exchange"),
            source=payload.get("source"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "last_trade_price": self.last_trade_price,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "mid_price": self.mid_price,
            "spread_abs": self.spread_abs,
            "spread_pct": self.spread_pct,
            "quote_available": self.quote_available,
            "quote_stale": self.quote_stale,
            "quote_timestamp": self.quote_timestamp.isoformat() if self.quote_timestamp else None,
            "trade_timestamp": self.trade_timestamp.isoformat() if self.trade_timestamp else None,
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "quote_age_seconds": self.quote_age_seconds,
            "fallback_pricing_used": self.fallback_pricing_used,
            "price_source_used": self.price_source_used,
            "evaluation_price": self.evaluation_price,
            "session_state": self.session_state,
            "exchange": self.exchange,
            "source": self.source,
        }


@dataclass(slots=True)
class RankedOpportunity:
    symbol: str
    asset_class: AssetClass
    name: str
    last_price: float | None
    price_change_pct: float | None
    momentum_score: float
    volatility_score: float
    liquidity_score: float
    spread_score: float
    tradability_score: float
    signal_quality_score: float
    regime_state: str
    tags: list[str] = field(default_factory=list)
    reason: str | None = None
    generated_at: datetime = field(default_factory=datetime.utcnow)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "name": self.name,
            "last_price": self.last_price,
            "price_change_pct": self.price_change_pct,
            "momentum_score": self.momentum_score,
            "volatility_score": self.volatility_score,
            "liquidity_score": self.liquidity_score,
            "spread_score": self.spread_score,
            "tradability_score": self.tradability_score,
            "signal_quality_score": self.signal_quality_score,
            "regime_state": self.regime_state,
            "tags": list(self.tags),
            "reason": self.reason,
            "generated_at": self.generated_at.isoformat(),
            "metrics": self.metrics,
        }
