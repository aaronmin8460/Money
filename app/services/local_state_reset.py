from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import inspect

from app.db.init_db import init_db
from app.db.models import Base
from app.db.session import get_engine
from app.monitoring.logger import get_logger
from app.services.runtime import RuntimeContainer, get_runtime

logger = get_logger("local_state_reset")


@dataclass
class LocalStateResetOptions:
    close_positions: bool = True
    cancel_open_orders: bool = True
    wipe_local_db: bool = False
    reset_daily_baseline_to_current_equity: bool = True


def reset_local_state(
    options: LocalStateResetOptions | None = None,
    *,
    runtime: RuntimeContainer | None = None,
) -> dict[str, Any]:
    resolved_options = options or LocalStateResetOptions()
    runtime = runtime or get_runtime()
    settings = runtime.settings

    if settings.is_live_enabled:
        raise ValueError("Local reset is blocked while live trading is enabled.")

    trader = runtime.get_auto_trader()
    was_running = trader.get_status()["running"]
    if was_running:
        trader.stop()

    canceled_orders: list[dict[str, Any]] = []
    if resolved_options.cancel_open_orders:
        canceled_orders = runtime.broker.cancel_open_orders()

    closed_positions: list[dict[str, Any]] = []
    if resolved_options.close_positions:
        closed_positions = runtime.broker.close_all_positions()

    if settings.is_mock_mode and hasattr(runtime.broker, "reset_state"):
        runtime.broker.reset_state(
            clear_orders=True,
            clear_positions=resolved_options.close_positions,
        )

    broker_positions = runtime.broker.get_positions()
    broker_account = runtime.broker.get_account()

    runtime.portfolio.reset_runtime_state(
        cash=broker_account.cash,
        equity=broker_account.equity,
        reset_daily_baseline_to_current_equity=resolved_options.reset_daily_baseline_to_current_equity,
    )
    if broker_positions:
        runtime.portfolio.reconcile_positions(broker_positions)
        runtime.portfolio.sync_account_state(broker_account.cash, broker_account.equity)
    runtime.portfolio.equity_history.clear()
    if resolved_options.reset_daily_baseline_to_current_equity:
        runtime.portfolio.reset_daily_baseline(equity=broker_account.equity)
        runtime.portfolio.equity_history.append(float(broker_account.equity))
    else:
        runtime.portfolio.daily_baseline_equity = None
        runtime.portfolio.daily_baseline_date = None

    runtime.risk_manager.clear_runtime_state()
    trader.reset_runtime_state()
    runtime.tranche_state.clear_all()

    wiped_tables: list[str] = []
    if resolved_options.wipe_local_db:
        wiped_tables = wipe_local_sqlite_history(settings.database_url)

    result = {
        "mode": settings.broker_mode,
        "paper_safe": not settings.is_live_enabled,
        "options": asdict(resolved_options),
        "auto_trader_was_running": was_running,
        "auto_trader_running": trader.get_status()["running"],
        "canceled_orders": canceled_orders,
        "closed_positions": closed_positions,
        "broker_account": {
            "cash": broker_account.cash,
            "equity": broker_account.equity,
            "buying_power": broker_account.buying_power,
            "positions": broker_account.positions,
            "mode": broker_account.mode,
            "trading_enabled": broker_account.trading_enabled,
        },
        "broker_positions": broker_positions,
        "local_portfolio_positions": runtime.portfolio.positions_diagnostics(),
        "daily_baseline_equity": runtime.portfolio.daily_baseline_equity,
        "daily_baseline_date": (
            runtime.portfolio.daily_baseline_date.isoformat()
            if runtime.portfolio.daily_baseline_date is not None
            else None
        ),
        "latest_rejection": runtime.risk_manager.get_rejection_snapshot(limit=1)["latest"],
        "tranche_state": runtime.tranche_state.snapshot(),
        "local_db_wiped": bool(wiped_tables),
        "wiped_tables": wiped_tables,
    }
    logger.info("Completed local state reset", extra=result)
    return result


def wipe_local_sqlite_history(database_url: str) -> list[str]:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("Local DB wipe is only supported for SQLite databases.")

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    Base.metadata.drop_all(bind=engine)
    init_db()
    return existing_tables
