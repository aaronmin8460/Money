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
from app.config.settings import get_settings
from app.data.historical import load_csv_data
from app.execution.execution_service import ExecutionService
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.backtest import run_backtest
from app.services.broker import BrokerAuthError, BrokerConnectionError, BrokerUpstreamError, create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.ema_crossover import EMACrossoverStrategy

logger = get_logger("api")
router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": get_settings().broker_mode}


@router.get("/config")
def config() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app_env": settings.app_env,
        "broker_mode": settings.broker_mode,
        "trading_enabled": settings.trading_enabled,
        "default_symbols": settings.default_symbols,
        "max_risk_per_trade": settings.max_risk_per_trade,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_drawdown_pct": settings.max_drawdown_pct,
        "max_positions": settings.max_positions,
    }


@router.get("/broker/status", response_model=BrokerStatus)
def broker_status() -> BrokerStatus:
    settings = get_settings()
    return BrokerStatus(
        broker_mode=settings.broker_mode,
        trading_enabled=settings.trading_enabled,
        has_credentials=settings.has_alpaca_credentials,
        safe_dry_run=not settings.trading_enabled or not settings.is_alpaca_mode,
        broker_label="Alpaca Paper" if settings.is_alpaca_mode else "Paper Mock",
    )


@router.get("/broker/account", response_model=AccountSummary)
def broker_account() -> AccountSummary:
    settings = get_settings()
    try:
        broker = create_broker(settings)
        return broker.get_account()
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
    settings = get_settings()
    broker = create_broker(settings)
    return broker.get_positions()


@router.get("/orders")
def orders() -> list[dict[str, Any]]:
    settings = get_settings()
    broker = create_broker(settings)
    return broker.list_orders()


@router.get("/trades")
def trades() -> list[Any]:
    return []


@router.get("/risk")
def risk() -> dict[str, Any]:
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio)
    return {
        "risk_events": portfolio.risk_events,
        "allowed": bool(risk_manager.settings.trading_enabled),
    }


@router.post("/run-once", response_model=RunOnceResult)
def run_once(request: RunOnceRequest = Body(...)) -> dict[str, Any]:
    settings = get_settings()
    try:
        broker = create_broker(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    symbol = request.symbol or (settings.default_symbols[0] if settings.default_symbols else "AAPL")
    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio)
    strategy = EMACrossoverStrategy()
    market_data_service = (
        AlpacaMarketDataService(settings)
        if settings.is_alpaca_mode
        else CSVMarketDataService()
    )
    exec_service = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=not settings.trading_enabled,
        market_data_service=market_data_service,
    )

    symbol_data: Any | None = None
    if settings.is_alpaca_mode:
        try:
            symbol_data = market_data_service.fetch_bars(symbol, limit=50)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to load Alpaca market data for {symbol}: {exc}",
            )
    else:
        sample_path = Path("data/sample.csv")
        fallback_path = Path("data/sample-fallback.csv")
        if sample_path.exists():
            symbol_data = load_csv_data(sample_path)
        elif fallback_path.exists():
            symbol_data = load_csv_data(fallback_path)
        else:
            raise HTTPException(
                status_code=404,
                detail="No local sample CSV data available for run-once.",
            )

    result = exec_service.run_once(symbol, strategy, symbol_data)
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
