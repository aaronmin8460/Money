from __future__ import annotations

import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

from app.services.backtest import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline replay/backtest comparison.")
    parser.add_argument("--symbol", help="Symbol to backtest", default="SPY")
    parser.add_argument("--csv-path", help="Path to historical CSV", default="data/sample.csv")
    parser.add_argument(
        "--mode",
        choices=["baseline", "candidate", "compare"],
        default="compare",
        help="Replay variant to run.",
    )
    parser.add_argument("--output-dir", help="Where to write research artifacts.", default="")
    args = parser.parse_args()

    result = run_backtest(
        Path(args.csv_path),
        args.symbol,
        mode=args.mode,
        compare=args.mode == "compare",
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
