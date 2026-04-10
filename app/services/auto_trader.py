from __future__ import annotations

import datetime
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.config.settings import Settings, get_settings
from app.db.models import AutoTraderRun
from app.db.session import SessionLocal
from app.execution.execution_service import ExecutionService
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import BrokerInterface, create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.base import Signal, TradeSignal
from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy

if TYPE_CHECKING:
    from app.services.runtime import RuntimeContainer

logger = get_logger("auto_trader")


class AutoTrader:
    """Automated trading service for periodic symbol scanning and order execution."""

    def __init__(self, settings: Settings | None = None, runtime: RuntimeContainer | None = None):
        self.settings = settings or get_settings()
        self.runtime = runtime

        if runtime is None:
            self.market_data_service = (
                AlpacaMarketDataService(self.settings)
                if self.settings.is_alpaca_mode
                else CSVMarketDataService()
            )
            self.broker: BrokerInterface = create_broker(
                self.settings,
                market_data_service=self.market_data_service if self.settings.is_paper_mode else None,
            )
            self.portfolio = Portfolio()
            self.risk_manager = RiskManager(self.portfolio, settings=self.settings, broker=self.broker)
            self.strategy = RegimeMomentumBreakoutStrategy()
            self.exec_service = ExecutionService(
                broker=self.broker,
                portfolio=self.portfolio,
                risk_manager=self.risk_manager,
                dry_run=not self.settings.trading_enabled,
                market_data_service=self.market_data_service,
            )
            self._execution_lock = threading.RLock()
        else:
            self.broker = runtime.broker
            self.portfolio = runtime.portfolio
            self.risk_manager = runtime.risk_manager
            self.strategy = runtime.strategy
            self.market_data_service = runtime.market_data_service
            self.exec_service = runtime.execution_service
            self._execution_lock = runtime.lock

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()
        self._last_run_time: Optional[datetime.datetime] = None
        self._last_scanned_symbols: List[str] = []
        self._last_signals: Dict[str, Any] = {}
        self._last_order: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_ranked_candidates: List[Dict[str, Any]] = []
        self._last_regime_snapshot: Dict[str, Any] = {}
        self._symbol_cooldowns: Dict[str, datetime.datetime] = {}
        self._market_open: bool = True

    def start(self) -> bool:
        """Start the auto-trading loop. Returns True if started, False if already running."""
        with self._state_lock:
            if self._running or (self._thread and self._thread.is_alive()):
                logger.warning("Auto-trader is already running")
                return False

            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="money-auto-trader",
            )
            self._thread.start()

        logger.info("Auto-trader started")
        return True

    def stop(self) -> bool:
        """Stop the auto-trading loop. Returns True if stopped, False if not running."""
        with self._state_lock:
            thread = self._thread
            if not self._running and not (thread and thread.is_alive()):
                logger.warning("Auto-trader is not running")
                self._thread = None
                return False

            self._running = False

        if thread:
            thread.join(timeout=5.0)

        with self._state_lock:
            self._thread = None

        logger.info("Auto-trader stopped")
        return True

    def run_now(self) -> Dict[str, Any]:
        """Run one cycle immediately and return the results."""
        try:
            results = self._scan_and_trade(self.settings.default_symbols)
            with self._state_lock:
                self._last_run_time = datetime.datetime.utcnow()
                self._last_error = None
            return {"success": True, "results": results}
        except Exception as exc:
            error_msg = f"Run-now failed: {exc}"
            logger.error(error_msg)
            with self._state_lock:
                self._last_error = error_msg
            return {"success": False, "error": error_msg}

    def run_symbol_now(self, symbol: str) -> Dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        now = datetime.datetime.utcnow()
        try:
            with self._execution_lock:
                self._prepare_cycle_state([normalized_symbol], now)
                self._sync_portfolio_from_broker()
                benchmark_data = self._load_benchmark_data()
                signal = self._evaluate_symbol(normalized_symbol, benchmark_data, now, raise_fetch_errors=True)
                execution = self.exec_service.process_signal(signal)
                result = self._build_execution_result(normalized_symbol, signal, execution)
                if execution.get("order"):
                    self._record_order(normalized_symbol, execution["order"], now)
                self._persist_run([result])

            with self._state_lock:
                self._last_run_time = now
                self._last_error = None
            return result
        except Exception as exc:
            with self._state_lock:
                self._last_error = f"Run-once failed for {normalized_symbol}: {exc}"
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the auto-trader."""
        with self._state_lock:
            return {
                "running": self._running,
                "last_run_time": self._last_run_time.isoformat() if self._last_run_time else None,
                "last_scanned_symbols": self._last_scanned_symbols,
                "last_signals": self._last_signals,
                "last_order": self._last_order,
                "last_error": self._last_error,
                "last_ranked_candidates": self._last_ranked_candidates,
                "last_regime_snapshot": self._last_regime_snapshot,
                "symbol_cooldowns": {
                    symbol: cooldown.isoformat()
                    for symbol, cooldown in self._symbol_cooldowns.items()
                },
                "market_open": self._market_open,
                "broker_mode": self.settings.broker_mode,
                "trading_enabled": self.settings.trading_enabled,
                "open_positions_count": len(self.portfolio.positions),
            }

    def _run_loop(self) -> None:
        """Main loop for periodic scanning."""
        while True:
            with self._state_lock:
                if not self._running:
                    break

            try:
                self._scan_and_trade(self.settings.default_symbols)
                with self._state_lock:
                    self._last_run_time = datetime.datetime.utcnow()
                    self._last_error = None
            except Exception as exc:
                error_msg = f"Auto-trader cycle failed: {exc}"
                logger.error(error_msg)
                with self._state_lock:
                    self._last_error = error_msg

            time.sleep(self.settings.scan_interval_seconds)

    def _prepare_cycle_state(self, symbols: List[str], now: datetime.datetime) -> None:
        with self._state_lock:
            self._last_scanned_symbols = symbols.copy()
            self._last_ranked_candidates = []
            self._last_signals = {}
            self._last_regime_snapshot = {
                "benchmark_symbol": self.strategy.regime_symbol,
                "market_open": self._market_open,
                "generated_at": now.isoformat(),
            }

    def _sync_portfolio_from_broker(self) -> None:
        try:
            positions = self.broker.get_positions()
            self.portfolio.reconcile_positions(positions)
        except Exception as exc:
            logger.warning("Failed to reconcile portfolio positions: %s", exc)

        try:
            account = self.broker.get_account()
            self.portfolio.sync_account_state(account.cash, account.equity)
        except Exception as exc:
            logger.warning("Failed to refresh portfolio cash/equity: %s", exc)

        if self.settings.is_alpaca_mode and self.settings.trading_enabled:
            try:
                self._market_open = self.broker.is_market_open()
            except Exception as exc:
                logger.warning("Failed to check market open: %s", exc)
                self._market_open = False
        else:
            self._market_open = True

    def _load_benchmark_data(self) -> Any | None:
        if not self.strategy.regime_symbol:
            return None

        try:
            return self.market_data_service.fetch_bars(
                self.strategy.regime_symbol,
                limit=max(self.strategy.regime_long_sma, self.strategy.return_3m_window) + 20,
            )
        except Exception as exc:
            logger.warning(
                "Failed to fetch benchmark bars for %s: %s",
                self.strategy.regime_symbol,
                exc,
            )
            return None

    def _evaluate_symbol(
        self,
        symbol: str,
        benchmark_data: Any | None,
        now: datetime.datetime,
        raise_fetch_errors: bool = False,
    ) -> TradeSignal:
        try:
            bars = self.market_data_service.fetch_bars(symbol, limit=260)
        except Exception as exc:
            error_message = str(exc)
            with self._state_lock:
                self._last_signals[symbol] = {
                    "symbol": symbol,
                    "signal": Signal.HOLD.value,
                    "action": "error",
                    "error": error_message,
                    "generated_at": now.isoformat(),
                }
            if raise_fetch_errors:
                raise
            return TradeSignal(symbol=symbol, signal=Signal.HOLD, reason=error_message)

        input_data: Any = {"symbol": bars, "benchmark": benchmark_data} if benchmark_data is not None else bars
        signals = self.strategy.generate_signals(symbol, input_data)
        latest_signal = signals[-1] if signals else TradeSignal(
            symbol=symbol,
            signal=Signal.HOLD,
            reason="Strategy returned no signals.",
        )

        signal_status = self._build_signal_status(symbol, latest_signal, now)
        with self._state_lock:
            self._last_signals[symbol] = signal_status
            self._last_regime_snapshot = {
                "benchmark_symbol": self.strategy.regime_symbol,
                "market_open": self._market_open,
                "generated_at": now.isoformat(),
            }
        return latest_signal

    def _build_signal_status(
        self,
        symbol: str,
        signal: TradeSignal,
        now: datetime.datetime,
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "signal": signal.signal.value,
            "price": signal.price,
            "strength": signal.strength,
            "reason": signal.reason,
            "atr": signal.atr,
            "stop_price": signal.stop_price,
            "trailing_stop": signal.trailing_stop,
            "momentum_score": signal.momentum_score,
            "regime_state": signal.regime_state,
            "timestamp": signal.timestamp,
            "generated_at": now.isoformat(),
        }

    def _build_execution_result(
        self,
        symbol: str,
        signal: TradeSignal,
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "signal": signal.signal.value,
            "latest_price": execution.get("latest_price"),
            "proposal": execution.get("proposal", {}),
            "risk": execution.get("risk"),
            "action": execution.get("action"),
            "order": execution.get("order"),
        }

    def _record_order(
        self,
        symbol: str,
        order: Dict[str, Any],
        now: datetime.datetime,
    ) -> None:
        with self._state_lock:
            self._last_order = order
            self._symbol_cooldowns[symbol] = now + datetime.timedelta(
                seconds=self.settings.cooldown_seconds_per_symbol
            )

    def _scan_and_trade(self, symbols: List[str]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        now = datetime.datetime.utcnow()
        normalized_symbols = [symbol.strip().upper() for symbol in symbols]

        with self._execution_lock:
            self._prepare_cycle_state(normalized_symbols, now)
            self._sync_portfolio_from_broker()
            benchmark_data = self._load_benchmark_data()

            buy_candidates: List[Dict[str, Any]] = []
            sell_candidates: List[Dict[str, Any]] = []

            for symbol in normalized_symbols:
                cooldown_end = self._symbol_cooldowns.get(symbol)
                if cooldown_end and now < cooldown_end:
                    results.append(
                        {
                            "symbol": symbol,
                            "action": "cooldown",
                            "cooldown_until": cooldown_end.isoformat(),
                        }
                    )
                    continue

                signal = self._evaluate_symbol(symbol, benchmark_data, now)
                signal_status = self._last_signals[symbol]

                if signal.signal == Signal.HOLD:
                    results.append(signal_status)
                    continue

                candidate = {"symbol": symbol, "signal": signal, "status": signal_status}
                if signal.signal == Signal.SELL:
                    sell_candidates.append(candidate)
                else:
                    buy_candidates.append(candidate)

            sorted_buy_candidates = sorted(
                buy_candidates,
                key=lambda candidate: candidate["signal"].momentum_score or 0.0,
                reverse=True,
            )
            with self._state_lock:
                self._last_ranked_candidates = [
                    {
                        "symbol": candidate["symbol"],
                        "momentum_score": candidate["signal"].momentum_score,
                        "reason": candidate["signal"].reason,
                        "signal": candidate["signal"].signal.value,
                    }
                    for candidate in sorted_buy_candidates
                ]

            available_slots = max(0, self.settings.max_positions - len(self.portfolio.positions))
            buy_queue = sorted_buy_candidates[:available_slots]
            skipped_buys = sorted_buy_candidates[available_slots:]

            for candidate in skipped_buys:
                results.append(
                    {
                        "symbol": candidate["symbol"],
                        "signal": candidate["signal"].signal.value,
                        "action": "skipped_by_rank",
                        "reason": "No available position slot",
                    }
                )

            if not self._market_open and self.settings.is_alpaca_mode and self.settings.trading_enabled:
                for candidate in buy_queue + sell_candidates:
                    results.append(
                        {
                            "symbol": candidate["symbol"],
                            "signal": candidate["signal"].signal.value,
                            "action": "market_closed",
                        }
                    )
                self._persist_run(results, error_message="Market closed")
                return results

            for candidate in buy_queue + sell_candidates:
                execution = self.exec_service.process_signal(candidate["signal"])
                action_result = self._build_execution_result(
                    candidate["symbol"],
                    candidate["signal"],
                    execution,
                )
                results.append(action_result)
                if execution.get("order"):
                    self._record_order(candidate["symbol"], execution["order"], now)

            self._persist_run(results)
            return results

    def _persist_run(self, results: List[Dict[str, Any]], error_message: str | None = None) -> None:
        try:
            with SessionLocal() as session:
                session.add(
                    AutoTraderRun(
                        symbols_scanned=json.dumps(self._last_scanned_symbols),
                        signals_generated=json.dumps(self._last_signals, default=str),
                        orders_submitted=json.dumps(
                            [result.get("order") for result in results if result.get("order")],
                            default=str,
                        ),
                        error_message=error_message,
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist auto-trader run record: %s", exc)


# Global instance
_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    from app.services.runtime import get_runtime

    _auto_trader = get_runtime().get_auto_trader()
    return _auto_trader
