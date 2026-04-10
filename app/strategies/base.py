from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from app.domain.models import AssetClass, AssetMetadata, MarketSessionStatus, QuoteSnapshot, SignalDirection


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategyContext:
    asset: AssetMetadata
    session: MarketSessionStatus | None = None
    quote: QuoteSnapshot | None = None
    timeframe: str = "1D"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeSignal:
    symbol: str
    signal: Signal
    asset_class: AssetClass = AssetClass.UNKNOWN
    strategy_name: str = "unknown"
    signal_type: str = "entry"
    direction: SignalDirection = SignalDirection.FLAT
    strength: float = 0.0
    confidence_score: float | None = None
    price: float | None = None
    entry_price: float | None = None
    reason: str | None = None
    timestamp: str | None = None
    atr: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    position_size: float | None = None
    trailing_stop: float | None = None
    momentum_score: float | None = None
    liquidity_score: float | None = None
    spread_score: float | None = None
    regime_state: str | None = None
    generated_at: datetime = field(default_factory=datetime.utcnow)
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.entry_price is None and self.price is not None:
            self.entry_price = self.price
        if self.price is None and self.entry_price is not None:
            self.price = self.entry_price
        if self.confidence_score is None:
            self.confidence_score = max(0.0, min(1.0, abs(self.strength)))
        if self.signal == Signal.BUY:
            self.direction = SignalDirection.LONG
        elif self.signal == Signal.SELL:
            self.direction = SignalDirection.SHORT
        else:
            self.direction = SignalDirection.FLAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "signal": self.signal.value,
            "asset_class": self.asset_class.value,
            "strategy_name": self.strategy_name,
            "signal_type": self.signal_type,
            "direction": self.direction.value,
            "strength": self.strength,
            "confidence_score": self.confidence_score,
            "price": self.price,
            "entry_price": self.entry_price,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "atr": self.atr,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "position_size": self.position_size,
            "trailing_stop": self.trailing_stop,
            "momentum_score": self.momentum_score,
            "liquidity_score": self.liquidity_score,
            "spread_score": self.spread_score,
            "regime_state": self.regime_state,
            "generated_at": self.generated_at.isoformat(),
            "metrics": self.metrics,
        }


class BaseStrategy(ABC):
    """Base class for trading strategies."""

    name: str = "base"
    supported_asset_classes: set[AssetClass] = {AssetClass.EQUITY}
    signal_only: bool = False

    def supports(self, asset_class: AssetClass) -> bool:
        return asset_class in self.supported_asset_classes

    @abstractmethod
    def generate_signals(
        self,
        symbol: str,
        data: Any,
        context: StrategyContext | None = None,
    ) -> list[TradeSignal]:
        raise NotImplementedError
