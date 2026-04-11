from __future__ import annotations

from typing import Any

from app.monitoring.logger import get_logger
from app.rl.replay_env import ReplayTradingEnv

logger = get_logger("rl.train_stub")


def train_stub(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Placeholder for offline RL experiments.

    This is intentionally disconnected from live and paper execution paths.
    """

    env = ReplayTradingEnv(history=history)
    observation = env.reset()
    steps = 0
    reward_total = 0.0
    done = False
    while not done and observation:
        step = env.step(action=0)
        reward_total += step.reward
        done = step.done
        observation = step.observation
        steps += 1
    logger.info("RL sandbox stub completed", extra={"steps": steps, "reward_total": reward_total})
    return {"steps": steps, "reward_total": reward_total, "experimental_only": True}
