from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TradingReplayStep:
    observation: dict[str, Any]
    reward: float
    done: bool
    info: dict[str, Any]
