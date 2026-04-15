from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from app.api.rate_limit import rate_limit_admin, rate_limit_default, rate_limit_signals
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
@rate_limit_default()
def broker_status(request: Request, response: Response) -> BrokerStatus:
    settings = get_runtime().settings
    return BrokerStatus(
        broker_mode=settings.broker_mode,
        broker_backend=settings.broker_backend,
        trading_enabled=settings.trading_enabled,
        has_credentials=settings.has_alpaca_credentials,
        safe_dry_run=not settings.trading_enabled,
        broker_label="Alpaca Paper" if settings.is_alpaca_mode else "Local Mock",
        live_trading_enabled=settings.live_trading_enabled,
        trading_profile=settings.effective_trading_profile,
    )


@router.get("/broker/account", response_model=AccountSummary)
@rate_limit_default()
def broker_account(request: Request, response: Response) -> AccountSummary:
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
@rate_limit_default()
def positions(request: Request, response: Response) -> list[dict[str, Any]]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return runtime.broker.get_positions()


@router.get("/orders")
@rate_limit_default()
def orders(request: Request, response: Response) -> list[dict[str, Any]]:
    runtime = get_runtime()
    return runtime.broker.list_orders()


@router.get("/trades")
@rate_limit_default()
def trades(request: Request, response: Response) -> list[dict[str, Any]]:
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
@rate_limit_default()
def risk(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return runtime.risk_manager.get_runtime_snapshot()


@router.post("/run-once", response_model=RunOnceResult)
@rate_limit_admin()
def run_once(request: Request, response: Response, payload: RunOnceRequest = Body(default=RunOnceRequest())) -> dict[str, Any]:
    runtime = get_runtime()
    if not payload.symbol or not payload.symbol.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Symbol is required. Please provide a symbol in the request body, e.g., "
                '{"symbol": "BTC/USD"} or {"symbol": "AAPL"} or as a query parameter ?symbol=AAPL'
            ),
        )
    symbol = payload.symbol.strip().upper()
    trader = runtime.get_auto_trader()
    try:
        result = trader.run_symbol_now(symbol, payload.asset_class)
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
@rate_limit_admin()
def backtest(request: Request, response: Response, payload: BacktestRequest) -> dict[str, Any]:
    csv_path = payload.csv_path or "data/sample.csv"
    path = Path(csv_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"CSV file not found: {csv_path}")

    try:
        result = run_backtest(path, payload.symbol)
    except Exception as exc:
        logger.error("Backtest failed", exc_info=exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return result


@router.get("/auto/status")
@rate_limit_signals()
def auto_status(request: Request, response: Response) -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return trader.get_status()


@router.post("/auto/start")
@rate_limit_admin()
def auto_start(request: Request, response: Response) -> dict[str, str]:
    trader = get_runtime().get_auto_trader()
    if trader.start():
        return {"message": "Auto-trader started"}
    return {"message": "Auto-trader is already running"}


@router.post("/auto/stop")
@rate_limit_admin()
def auto_stop(request: Request, response: Response) -> dict[str, str]:
    trader = get_runtime().get_auto_trader()
    if trader.stop():
        return {"message": "Auto-trader stopped"}
    return {"message": "Auto-trader is not running"}


@router.post("/auto/run-now")
@rate_limit_admin()
def auto_run_now(request: Request, response: Response) -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return trader.run_now()


@router.get("/strategy/signals")
@rate_limit_signals()
def strategy_signals(request: Request, response: Response) -> dict[str, Any]:
    trader = get_runtime().get_auto_trader()
    return {"signals": trader.get_status()["last_signals"]}


@router.get("/strategy/positions")
@rate_limit_signals()
def strategy_positions(request: Request, response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    runtime.sync_with_broker()
    return {"positions": runtime.portfolio.positions_snapshot()}
