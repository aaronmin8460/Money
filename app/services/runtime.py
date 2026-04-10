from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.settings import Settings, get_settings
from app.execution.execution_service import ExecutionService
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.asset_catalog import AssetCatalogService
from app.services.broker import BrokerInterface, create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService, MarketDataService
from app.services.market_overview import MarketOverviewService
from app.services.scanner import ScannerService
from app.services.tranche_state import TrancheStateStore
from app.strategies.base import BaseStrategy
from app.strategies.registry import StrategyRegistry, build_strategy_registry

if TYPE_CHECKING:
    from app.services.auto_trader import AutoTrader

logger = get_logger("runtime")


@dataclass
class RuntimeContainer:
    settings: Settings
    broker: BrokerInterface
    portfolio: Portfolio
    risk_manager: RiskManager
    market_data_service: MarketDataService
    asset_catalog: AssetCatalogService
    scanner: ScannerService
    market_overview: MarketOverviewService
    strategy_registry: StrategyRegistry
    execution_service: ExecutionService
    strategy: BaseStrategy
    tranche_state: TrancheStateStore
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _auto_trader: AutoTrader | None = field(default=None, init=False, repr=False)

    def sync_with_broker(self) -> None:
        with self.lock:
            try:
                positions = self.broker.get_positions()
                self.portfolio.reconcile_positions(positions)
            except Exception as exc:
                logger.warning("Failed to reconcile runtime positions: %s", exc)

            try:
                account = self.broker.get_account()
                self.portfolio.sync_account_state(account.cash, account.equity)
            except Exception as exc:
                logger.warning("Failed to refresh runtime account state: %s", exc)

            try:
                self.asset_catalog.ensure_fresh()
            except Exception as exc:
                logger.warning("Failed to refresh asset catalog: %s", exc)

    def get_auto_trader(self) -> AutoTrader:
        if self._auto_trader is None:
            from app.services.auto_trader import AutoTrader

            self._auto_trader = AutoTrader(settings=self.settings, runtime=self)
        return self._auto_trader

    def shutdown(self) -> None:
        if self._auto_trader is not None:
            self._auto_trader.stop()
            self._auto_trader = None
        close = getattr(self.broker, "close", None)
        if callable(close):
            close()
        market_data_close = getattr(self.market_data_service, "close", None)
        if callable(market_data_close):
            market_data_close()


_runtime: RuntimeContainer | None = None
_runtime_lock = threading.Lock()


def _build_runtime(settings: Settings) -> RuntimeContainer:
    market_data_service: MarketDataService
    if settings.is_alpaca_mode:
        market_data_service = AlpacaMarketDataService(settings)
        broker = create_broker(settings)
    else:
        market_data_service = CSVMarketDataService()
        broker = create_broker(settings, market_data_service=market_data_service)

    portfolio = Portfolio()
    risk_manager = RiskManager(portfolio, settings=settings, broker=broker)
    asset_catalog = AssetCatalogService(broker=broker, settings=settings)
    scanner = ScannerService(asset_catalog=asset_catalog, market_data_service=market_data_service, settings=settings)
    market_overview = MarketOverviewService(scanner)
    strategy_registry = build_strategy_registry(settings)
    strategy = strategy_registry.get(settings.active_strategy)
    execution_service = ExecutionService(
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        dry_run=not settings.trading_enabled,
        market_data_service=market_data_service,
        settings=settings,
        tranche_state=TrancheStateStore(),
    )
    runtime = RuntimeContainer(
        settings=settings,
        broker=broker,
        portfolio=portfolio,
        risk_manager=risk_manager,
        market_data_service=market_data_service,
        asset_catalog=asset_catalog,
        scanner=scanner,
        market_overview=market_overview,
        strategy_registry=strategy_registry,
        execution_service=execution_service,
        strategy=strategy,
        tranche_state=execution_service.tranche_state,
    )
    runtime.sync_with_broker()
    logger.info(
        "Runtime initialized",
        extra={
            "broker_mode": settings.broker_mode,
            "broker_backend": settings.broker_backend,
            "active_strategy": settings.active_strategy,
            "trading_enabled": settings.trading_enabled,
            "auto_trade_enabled": settings.auto_trade_enabled,
        },
    )
    return runtime


def get_runtime(settings: Settings | None = None) -> RuntimeContainer:
    global _runtime

    with _runtime_lock:
        resolved_settings = settings or get_settings()
        if _runtime is None:
            _runtime = _build_runtime(resolved_settings)
        return _runtime


def reset_runtime(settings: Settings | None = None) -> RuntimeContainer:
    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
        _runtime = _build_runtime(settings or get_settings())
        return _runtime


def close_runtime() -> None:
    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
            _runtime = None
