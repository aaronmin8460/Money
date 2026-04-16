from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401

from app.services.performance import calculate_performance_summary


def _format_metric(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _print_console_summary(summary: dict[str, object]) -> None:
    metrics = dict(summary.get("metrics") or {})
    drawdown = dict(metrics.get("max_drawdown") or {})
    counts = dict(summary.get("counts") or {})

    print("Performance Summary")
    print(f"Source: {summary.get('source')}")
    print(f"Status: {summary.get('status')}")
    print(f"Sharpe ratio: {_format_metric(metrics.get('sharpe_ratio'))}")
    print(f"Sortino ratio: {_format_metric(metrics.get('sortino_ratio'))}")
    print(
        "Max drawdown: "
        f"{_format_metric(drawdown.get('amount'))} "
        f"({_format_metric(drawdown.get('pct'))})"
    )
    print(
        "Coverage: "
        f"{counts.get('equity_snapshots', 0)} equity snapshots, "
        f"{counts.get('trades_with_pnl', 0)} trades with P&L, "
        f"{counts.get('fills', 0)} fills"
    )
    daily_pnl = list(summary.get("daily_pnl") or [])
    weekly_pnl = list(summary.get("weekly_pnl") or [])
    print(f"Daily P&L days: {len(daily_pnl)}")
    if daily_pnl:
        latest = daily_pnl[-1]
        print(f"Latest daily P&L: {latest['date']} {latest['pnl']}")
    print(f"Weekly P&L weeks: {len(weekly_pnl)}")
    if weekly_pnl:
        latest = weekly_pnl[-1]
        print(f"Latest weekly P&L: {latest['week']} {latest['pnl']}")
    warnings = list(summary.get("warnings") or [])
    if warnings:
        print("Notes:")
        for warning in warnings:
            print(f"- {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate local trading performance metrics.")
    parser.add_argument("--json", action="store_true", help="Print the raw JSON summary instead of console text.")
    args = parser.parse_args()

    summary = calculate_performance_summary()
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    _print_console_summary(summary)


if __name__ == "__main__":
    main()
