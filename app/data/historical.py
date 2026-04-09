from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_csv_data(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"Historical data file not found: {csv_path}")

    df = pd.read_csv(csv_path, parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df
