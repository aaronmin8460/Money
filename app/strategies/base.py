from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    symbol: str
    signal: Signal
    strength: float = 0.0
    price: float | None = None
    reason: str | None = None
    timestamp: str | None = None
    atr: float | None = None
    stop_price: float | None = None
    trailing_stop: float | None = None
    momentum_score: float | None = None
    regime_state: str | None = None


class BaseStrategy(ABC):
    """Base class for trading strategies."""

    @abstractmethod
    def generate_signals(self, symbol: str, data: Any) -> list[TradeSignal]:
        raise NotImplementedError
