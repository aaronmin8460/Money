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
