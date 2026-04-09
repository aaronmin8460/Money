from __future__ import annotations

import datetime
import json
import threading
import time
from typing import Any, Dict, List, Optional

from app.config.settings import Settings, get_settings
from app.db.models import AutoTraderRun
from app.db.session import SessionLocal
from app.execution.execution_service import ExecutionService
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import BrokerInterface, create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.base import Signal
from app.strategies.regime_momentum_breakout import RegimeMomentumBreakoutStrategy

logger = get_logger("auto_trader")


class AutoTrader:
    """Automated trading service for periodic symbol scanning and order execution."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.broker: BrokerInterface = create_broker(self.settings)
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager(self.portfolio)
        self.strategy = RegimeMomentumBreakoutStrategy()
        self.market_data_service = (
            AlpacaMarketDataService(self.settings)
            if self.settings.is_alpaca_mode
            else CSVMarketDataService()
        )
        self.exec_service = ExecutionService(
            broker=self.broker,
            portfolio=self.portfolio,
            risk_manager=self.risk_manager,
            dry_run=not self.settings.trading_enabled,
            market_data_service=self.market_data_service,
        )

        self._running = False
        self._thread: Optional[threading.Thread] = None
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
        if self._running or (self._thread and self._thread.is_alive()):
            logger.warning("Auto-trader is already running")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Auto-trader started")
        return True

    def stop(self) -> bool:
        """Stop the auto-trading loop. Returns True if stopped, False if not running."""
        if not self._running:
            logger.warning("Auto-trader is not running")
            return False

        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Auto-trader stopped")
        return True

    def run_now(self) -> Dict[str, Any]:
        """Run one cycle immediately and return the results."""
        try:
            results = self._scan_and_trade()
            self._last_run_time = datetime.datetime.utcnow()
            self._last_error = None
            return {"success": True, "results": results}
        except Exception as exc:
            error_msg = f"Run-now failed: {exc}"
            logger.error(error_msg)
            self._last_error = error_msg
            return {"success": False, "error": error_msg}

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the auto-trader."""
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
        }

    def _run_loop(self) -> None:
        """Main loop for periodic scanning."""
        while self._running:
            try:
                self._scan_and_trade()
                self._last_run_time = datetime.datetime.utcnow()
                self._last_error = None
            except Exception as exc:
                error_msg = f"Auto-trader cycle failed: {exc}"
                logger.error(error_msg)
                self._last_error = error_msg

            time.sleep(self.settings.scan_interval_seconds)

    def _scan_and_trade(self) -> List[Dict[str, Any]]:
        """Scan the configured universe, rank candidates, and execute approved trades."""
        results: List[Dict[str, Any]] = []
        now = datetime.datetime.utcnow()
        self._last_scanned_symbols = self.settings.default_symbols.copy()
        self._last_ranked_candidates = []
        self._last_signals = {}

        try:
            positions = self.broker.get_positions()
            self.portfolio.reconcile_positions(positions)
        except Exception as exc:
            logger.warning(f"Failed to reconcile portfolio positions: {exc}")

        if self.settings.is_alpaca_mode and self.settings.trading_enabled:
            try:
                self._market_open = self.broker.is_market_open()
            except Exception as exc:
                logger.warning(f"Failed to check market open: {exc}")
                self._market_open = False

        benchmark_data = None
        if self.strategy.regime_symbol:
            try:
                benchmark_data = self.market_data_service.fetch_bars(
                    self.strategy.regime_symbol,
                    limit=max(self.strategy.regime_long_sma, self.strategy.return_3m_window) + 20,
                )
            except Exception as exc:
                logger.warning(f"Failed to fetch benchmark bars for {self.strategy.regime_symbol}: {exc}")

        self._last_regime_snapshot = {
            "benchmark_symbol": self.strategy.regime_symbol,
            "market_open": self._market_open,
        }

        buy_candidates: List[Dict[str, Any]] = []
        sell_candidates: List[Dict[str, Any]] = []

        for symbol in self.settings.default_symbols:
            if symbol in self._symbol_cooldowns:
                cooldown_end = self._symbol_cooldowns[symbol]
                if now < cooldown_end:
                    results.append({"symbol": symbol, "action": "cooldown", "cooldown_until": cooldown_end.isoformat()})
                    continue

            try:
                bars = self.market_data_service.fetch_bars(symbol, limit=260)
            except Exception as exc:
                logger.error(f"Failed to fetch bars for {symbol}: {exc}")
                results.append({"symbol": symbol, "error": str(exc)})
                continue

            input_data: Any = {"symbol": bars, "benchmark": benchmark_data} if benchmark_data is not None else bars
            signals = self.strategy.generate_signals(symbol, input_data)
            latest_signal = signals[-1] if signals else None
            symbol_status: Dict[str, Any] = {"symbol": symbol, "signal": "HOLD", "action": "hold"}

            if latest_signal is None:
                latest_signal = None
            else:
                symbol_status = {
                    "symbol": symbol,
                    "signal": latest_signal.signal.value,
                    "price": latest_signal.price,
                    "strength": latest_signal.strength,
                    "reason": latest_signal.reason,
                    "atr": latest_signal.atr,
                    "stop_price": latest_signal.stop_price,
                    "trailing_stop": latest_signal.trailing_stop,
                    "momentum_score": latest_signal.momentum_score,
                    "regime_state": latest_signal.regime_state,
                    "timestamp": latest_signal.timestamp,
                }

            self._last_signals[symbol] = {
                **symbol_status,
                "generated_at": now.isoformat(),
            }

            if latest_signal is None or latest_signal.signal == Signal.HOLD:
                results.append(symbol_status)
                continue

            if latest_signal.signal == Signal.SELL:
                sell_candidates.append({"symbol": symbol, "signal": latest_signal, "status": symbol_status})
                continue

            buy_candidates.append({"symbol": symbol, "signal": latest_signal, "status": symbol_status})

        sorted_buy_candidates = sorted(
            buy_candidates,
            key=lambda candidate: candidate["signal"].momentum_score or 0.0,
            reverse=True,
        )
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

        if skipped_buys:
            for candidate in skipped_buys:
                symbol = candidate["symbol"]
                results.append({
                    "symbol": symbol,
                    "signal": candidate["signal"].signal.value,
                    "action": "skipped_by_rank",
                    "reason": "No available position slot",
                })

        if not self._market_open and self.settings.is_alpaca_mode and self.settings.trading_enabled:
            for candidate in buy_queue + sell_candidates:
                results.append({
                    "symbol": candidate["symbol"],
                    "signal": candidate["signal"].signal.value,
                    "action": "market_closed",
                })
            self._persist_run(results, error_message="Market closed")
            return results

        for candidate in buy_queue + sell_candidates:
            execution = self.exec_service.process_signal(candidate["signal"])
            action_result = {
                "symbol": candidate["symbol"],
                "signal": candidate["signal"].signal.value,
                "action": execution.get("action"),
                "risk": execution.get("risk"),
                "order": execution.get("order"),
            }
            results.append(action_result)
            if execution.get("order"):
                self._last_order = execution["order"]
                self._symbol_cooldowns[candidate["symbol"]] = now + datetime.timedelta(seconds=self.settings.cooldown_seconds_per_symbol)

        self._persist_run(results)
        return results

    def _persist_run(self, results: List[Dict[str, Any]], error_message: str | None = None) -> None:
        try:
            with SessionLocal() as session:
                session.add(
                    AutoTraderRun(
                        symbols_scanned=json.dumps(self._last_scanned_symbols),
                        signals_generated=json.dumps(self._last_signals, default=str),
                        orders_submitted=json.dumps([result.get("order") for result in results if result.get("order")], default=str),
                        error_message=error_message,
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning(f"Failed to persist auto-trader run record: {exc}")


# Global instance
_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    if _auto_trader is None:
        _auto_trader = AutoTrader()
    return _auto_trader