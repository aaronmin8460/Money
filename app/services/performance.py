from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from statistics import fmean, pstdev
from typing import Any, Iterable, Sequence

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import EquitySnapshot, FillRecord, Trade
from app.db.session import SessionLocal, get_engine

ANNUAL_TRADING_DAYS = 252


def _round_metric(value: float | None, places: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


def _round_money(value: float) -> float:
    return round(value, 2)


def calculate_max_drawdown(values: Sequence[float]) -> dict[str, float | None]:
    """Calculate drawdown from an equity-like curve.

    The returned amount is positive dollars/points below the prior peak. The
    percentage is only reported when that peak is positive.
    """
    clean_values = []
    for value in values:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_value):
            clean_values.append(numeric_value)
    if not clean_values:
        return {"amount": None, "pct": None, "peak": None, "trough": None}

    peak = clean_values[0]
    max_drawdown = 0.0
    drawdown_peak = peak
    drawdown_trough = peak
    for value in clean_values:
        if value > peak:
            peak = value
        drawdown = peak - value
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            drawdown_peak = peak
            drawdown_trough = value

    pct = (max_drawdown / drawdown_peak) if drawdown_peak > 0 else None
    return {
        "amount": _round_money(max_drawdown),
        "pct": _round_metric(pct),
        "peak": _round_money(drawdown_peak),
        "trough": _round_money(drawdown_trough),
    }


def _calculate_sharpe_ratio(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    volatility = pstdev(returns)
    if volatility <= 0:
        return None
    return fmean(returns) / volatility * math.sqrt(ANNUAL_TRADING_DAYS)


def _calculate_sortino_ratio(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    downside_returns = [min(return_value, 0.0) for return_value in returns]
    downside_deviation = math.sqrt(fmean(value * value for value in downside_returns))
    if downside_deviation <= 0:
        return None
    return fmean(returns) / downside_deviation * math.sqrt(ANNUAL_TRADING_DAYS)


def _iso_week(day: date) -> str:
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _aggregate_weekly_pnl(daily_pnl: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    weekly_totals: dict[str, float] = defaultdict(float)
    for row in daily_pnl:
        day = date.fromisoformat(row["date"])
        weekly_totals[_iso_week(day)] += float(row["pnl"])
    return [
        {"week": week, "pnl": _round_money(pnl)}
        for week, pnl in sorted(weekly_totals.items())
    ]


def _daily_equity_points(snapshots: Sequence[EquitySnapshot]) -> list[tuple[date, float]]:
    latest_by_day: dict[date, tuple[Any, float]] = {}
    for snapshot in snapshots:
        if snapshot.timestamp is None or snapshot.equity is None:
            continue
        day = snapshot.timestamp.date()
        existing = latest_by_day.get(day)
        if existing is None or snapshot.timestamp >= existing[0]:
            latest_by_day[day] = (snapshot.timestamp, float(snapshot.equity))
    return [(day, equity) for day, (_, equity) in sorted(latest_by_day.items())]


def _performance_from_equity_snapshots(
    snapshots: Sequence[EquitySnapshot],
) -> tuple[list[dict[str, Any]], list[float], dict[str, float | None]]:
    points = _daily_equity_points(snapshots)
    daily_pnl: list[dict[str, Any]] = []
    returns: list[float] = []
    previous_equity: float | None = None
    for day, equity in points:
        if previous_equity is not None:
            pnl = equity - previous_equity
            daily_pnl.append({"date": day.isoformat(), "pnl": _round_money(pnl)})
            if previous_equity != 0:
                returns.append(pnl / previous_equity)
        previous_equity = equity
    drawdown = calculate_max_drawdown([equity for _, equity in points])
    return daily_pnl, returns, drawdown


def _daily_trade_pnl(trades: Sequence[Trade]) -> list[dict[str, Any]]:
    totals: dict[date, float] = defaultdict(float)
    for trade in trades:
        if trade.pnl is None or trade.executed_at is None:
            continue
        totals[trade.executed_at.date()] += float(trade.pnl)
    return [
        {"date": day.isoformat(), "pnl": _round_money(pnl)}
        for day, pnl in sorted(totals.items())
    ]


def _cumulative_values(daily_pnl: Sequence[dict[str, Any]]) -> list[float]:
    running_total = 0.0
    values = [0.0]
    for row in daily_pnl:
        running_total += float(row["pnl"])
        values.append(running_total)
    return values


def calculate_performance_summary(session: Session | None = None) -> dict[str, Any]:
    """Build a compact performance summary from persisted local data.

    Assumptions are intentionally explicit:
    - Sharpe, Sortino, and percentage drawdown require daily equity snapshots.
    - Legacy Trade.pnl rows can support realized P&L aggregation only.
    - FillRecord rows are fills/cashflows without realized P&L and are not
      treated as performance by themselves.
    """
    owns_session = session is None
    if owns_session:
        get_engine()
        session = SessionLocal()
    assert session is not None

    assumptions = [
        "Sharpe, Sortino, and percentage drawdown use daily equity snapshot closes when available.",
        "The first daily equity close is treated as the baseline for daily P&L aggregation.",
        "Legacy Trade.pnl rows are used only for realized P&L aggregation when equity snapshots are unavailable.",
        "Fill records do not include realized P&L, so fill-only history is counted but not treated as performance.",
    ]

    try:
        equity_snapshots = session.query(EquitySnapshot).order_by(EquitySnapshot.timestamp.asc()).all()
        trades = session.query(Trade).order_by(Trade.executed_at.asc()).all()
        fills_count = session.query(FillRecord).count()

        trades_with_pnl = [trade for trade in trades if trade.pnl is not None]

        source = "insufficient_data"
        daily_pnl: list[dict[str, Any]] = []
        returns: list[float] = []
        drawdown = {"amount": None, "pct": None, "peak": None, "trough": None}
        warnings: list[str] = []

        if equity_snapshots:
            source = "equity_snapshots"
            daily_pnl, returns, drawdown = _performance_from_equity_snapshots(equity_snapshots)
            if len(returns) < 2:
                warnings.append("Need at least three daily equity snapshot closes to calculate Sharpe and Sortino.")
        elif trades_with_pnl:
            source = "legacy_trade_pnl"
            daily_pnl = _daily_trade_pnl(trades_with_pnl)
            drawdown = calculate_max_drawdown(_cumulative_values(daily_pnl))
            drawdown["pct"] = None
            warnings.append("No equity snapshots found; return ratios and percentage drawdown are unavailable.")
        else:
            warnings.append("No equity snapshots or legacy Trade.pnl rows found; performance metrics are partial.")
            if fills_count:
                warnings.append("Fill records exist, but fills do not contain realized P&L in this schema.")

        sharpe_ratio = _round_metric(_calculate_sharpe_ratio(returns))
        sortino_ratio = _round_metric(_calculate_sortino_ratio(returns))
        if returns and sharpe_ratio is None:
            warnings.append("Sharpe ratio unavailable because the return series has insufficient or zero volatility.")
        if returns and sortino_ratio is None:
            warnings.append("Sortino ratio unavailable because the return series has insufficient downside volatility.")

        weekly_pnl = _aggregate_weekly_pnl(daily_pnl)
        return {
            "status": "ok" if source == "equity_snapshots" and not warnings else "partial",
            "source": source,
            "metrics": {
                "sharpe_ratio": sharpe_ratio,
                "sortino_ratio": sortino_ratio,
                "max_drawdown": drawdown,
            },
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "counts": {
                "equity_snapshots": len(equity_snapshots),
                "trades": len(trades),
                "trades_with_pnl": len(trades_with_pnl),
                "fills": fills_count,
                "daily_pnl_days": len(daily_pnl),
                "weekly_pnl_weeks": len(weekly_pnl),
            },
            "assumptions": assumptions,
            "warnings": warnings,
        }
    except SQLAlchemyError as exc:
        return {
            "status": "partial",
            "source": "database_unavailable",
            "metrics": {
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "max_drawdown": {"amount": None, "pct": None, "peak": None, "trough": None},
            },
            "daily_pnl": [],
            "weekly_pnl": [],
            "counts": {
                "equity_snapshots": 0,
                "trades": 0,
                "trades_with_pnl": 0,
                "fills": 0,
                "daily_pnl_days": 0,
                "weekly_pnl_weeks": 0,
            },
            "assumptions": assumptions,
            "warnings": [f"Performance tables could not be read: {type(exc).__name__}."],
        }
    finally:
        if owns_session:
            session.close()
