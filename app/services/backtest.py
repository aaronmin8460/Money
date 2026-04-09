from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover


@dataclass
class BacktestResult:
    return_pct: float
    max_drawdown: float
    win_rate: float
    trades: int


class EMABacktestStrategy(Strategy):
    n1 = 12
    n2 = 26

    def init(self) -> None:
        self.ema_short = self.I(
            lambda close: close.ewm(span=self.n1, adjust=False).mean(),
            self.data.Close,
        )
        self.ema_long = self.I(
            lambda close: close.ewm(span=self.n2, adjust=False).mean(),
            self.data.Close,
        )

    def next(self) -> None:
        if crossover(self.ema_short, self.ema_long):
            self.buy()
        elif crossover(self.ema_long, self.ema_short):
            self.position.close()


def run_backtest(csv_path: Path, symbol: str | None = None) -> dict[str, Any]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV path not found: {csv_path}")

    df = pd.read_csv(csv_path, parse_dates=["Date"]).set_index("Date")
    bt = Backtest(df, EMABacktestStrategy, cash=100_000, commission=0.001)
    stats = bt.run()
    return {
        "return_pct": float(stats["Return [%]"]),
        "max_drawdown": float(stats["Max. Drawdown [%]"] if "Max. Drawdown [%]" in stats else stats.get("Max. Drawdown", 0.0)),
        "win_rate": float(stats["Win Rate [%]"] if "Win Rate [%]" in stats else stats.get("Win Rate", 0.0)),
        "trades": int(stats["# Trades"] if "# Trades" in stats else stats.get("Trades", 0)),
    }
