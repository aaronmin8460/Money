from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401

from app.services.backtest import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a sample EMA backtest.")
    parser.add_argument("--symbol", help="Symbol to backtest", default="SPY")
    parser.add_argument("--csv-path", help="Path to historical CSV", default="data/sample.csv")
    args = parser.parse_args()

    result = run_backtest(Path(args.csv_path), args.symbol)
    print("Backtest result:")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
