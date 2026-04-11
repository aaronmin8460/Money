from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.rl.env import TradingReplayStep


@dataclass
class ReplayTradingEnv:
    """Very small replay-only environment for offline RL experiments."""

    history: list[dict[str, Any]]
    index: int = 0
    position: int = 0

    def reset(self) -> dict[str, Any]:
        self.index = 0
        self.position = 0
        return self.history[0] if self.history else {}

    def step(self, action: int) -> TradingReplayStep:
        if not self.history:
            return TradingReplayStep(observation={}, reward=0.0, done=True, info={"reason": "empty_history"})

        current = self.history[self.index]
        next_index = min(self.index + 1, len(self.history) - 1)
        next_observation = self.history[next_index]
        current_price = float(current.get("close", current.get("Close", 0.0)) or 0.0)
        next_price = float(next_observation.get("close", next_observation.get("Close", current_price)) or current_price)

        reward = 0.0
        if action == 1:  # buy/long
            reward = next_price - current_price
            self.position = 1
        elif action == 2:  # flat/exit
            self.position = 0

        self.index = next_index
        done = self.index >= len(self.history) - 1
        return TradingReplayStep(
            observation=next_observation,
            reward=reward,
            done=done,
            info={"experimental_only": True, "position": self.position},
        )
