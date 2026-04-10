from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.api.schemas import (
    AccountSummary,
    BacktestRequest,
    BrokerStatus,
    RunOnceRequest,
    RunOnceResult,
)
from app.monitoring.logger import get_logger
from app.services.backtest import run_backtest
from app.services.broker import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerUpstreamError,
)
from app.services.runtime import get_runtime

logger = get_logger("api.trading")
router = APIRouter()


@router.get("/broker/status", response_model=BrokerStatus)
def broker_status() -> BrokerStatus:
    settings = get_runtime().settings
    return BrokerStatus(
        broker_mode=settings.broker_mode,
        broker_backend=settings.broker_backend,
        trading_enabled=settings.trading_enabled,
        has_credentials=settings.has_alpaca_credentials,
        safe_dry_run=not settings.trading_enabled,
        broker_label="Alpaca Paper" if settings.is_alpaca_mode else "Local Mock",
        live_trading_enabled=settings.live_trading_enabled,
    )


@router.get("/broker/account", response_model=AccountSummary)
def broker_account() -> AccountSummary:
    runtime = get_runtime()
    try:
        runtime.sync_with_broker()
        return runtime.broker.get_account()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except BrokerAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except BrokerUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except BrokerConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/positions")
def positions() -> list[dict[str, Any]]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return runtime.broker.get_positions()


@router.get("/orders")
def orders() -> list[dict[str, Any]]:
    runtime = get_runtime()
    return runtime.broker.list_orders()


@router.get("/trades")
def trades() -> list[dict[str, Any]]:
    from app.db.models import FillRecord
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rows = session.query(FillRecord).order_by(FillRecord.filled_at.desc()).limit(100).all()
    return [
        {
            "id": row.id,
            "order_id": row.order_id,
            "symbol": row.symbol,
            "asset_class": row.asset_class,
            "side": row.side,
            "quantity": row.quantity,
            "price": row.price,
            "status": row.status,
            "filled_at": row.filled_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/risk")
def risk() -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return runtime.risk_manager.get_runtime_snapshot()


@router.post("/run-once", response_model=RunOnceResult)
def run_once(request: RunOnceRequest = Body(...)) -> dict[str, Any]:
    runtime = get_runtime()
    symbol = request.symbol or (runtime.settings.manual_symbols[0] if runtime.settings.manual_symbols else "AAPL")
    trader = runtime.get_auto_trader()
    try:
        result = trader.run_symbol_now(symbol, request.asset_class)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except BrokerAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except BrokerUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except BrokerConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Run-once failed for {symbol}: {exc}")

    logger.info("Run once completed", extra={"result": result})
    return result


@router.post("/backtest")
def backtest(request: BacktestRequest) -> dict[str, Any]:
    csv_path = request.csv_path or "data/sample.csv"
    path = Path(csv_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")

    try:
        result = run_backtest(path, request.symbol)
    except Exception as exc:
        logger.error("Backtest failed", exc_info=exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return result


@router.get("/auto/status")
def auto_status() -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return trader.get_status()


@router.post("/auto/start")
def auto_start() -> dict[str, str]:
    trader = get_runtime().get_auto_trader()
    if trader.start():
        return {"message": "Auto-trader started"}
    return {"message": "Auto-trader is already running"}


@router.post("/auto/stop")
def auto_stop() -> dict[str, str]:
    trader = get_runtime().get_auto_trader()
    if trader.stop():
        return {"message": "Auto-trader stopped"}
    return {"message": "Auto-trader is not running"}


@router.post("/auto/run-now")
def auto_run_now() -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return trader.run_now()


@router.get("/strategy/signals")
def strategy_signals() -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return {"signals": trader.get_status()["last_signals"]}


@router.get("/strategy/positions")
def strategy_positions() -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return {"positions": runtime.portfolio.positions_snapshot()}
