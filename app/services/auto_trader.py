from __future__ import annotations

import asyncio
import datetime
import threading
import time
from typing import Any, Dict, List, Optional

from app.config.settings import Settings, get_settings
from app.execution.execution_service import ExecutionService
from app.monitoring.logger import get_logger
from app.portfolio.portfolio import Portfolio
from app.risk.risk_manager import RiskManager
from app.services.broker import BrokerInterface, OrderRequest, create_broker
from app.services.market_data import AlpacaMarketDataService, CSVMarketDataService
from app.strategies.ema_crossover import EMACrossoverStrategy

logger = get_logger("auto_trader")


class AutoTrader:
    """Automated trading service for periodic symbol scanning and order execution."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.broker: BrokerInterface = create_broker(self.settings)
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager(self.portfolio)
        self.strategy = EMACrossoverStrategy()
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
        self._symbol_cooldowns: Dict[str, datetime.datetime] = {}

    def start(self) -> bool:
        """Start the auto-trading loop. Returns True if started, False if already running."""
        if self._running:
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
            "symbol_cooldowns": {
                symbol: cooldown.isoformat()
                for symbol, cooldown in self._symbol_cooldowns.items()
            },
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
        """Scan symbols and execute trades."""
        results = []
        self._last_scanned_symbols = self.settings.default_symbols.copy()

        for symbol in self.settings.default_symbols:
            try:
                result = self._process_symbol(symbol)
                results.append(result)
            except Exception as exc:
                logger.error(f"Failed to process {symbol}: {exc}")
                results.append({"symbol": symbol, "error": str(exc)})

        return results

    def _process_symbol(self, symbol: str) -> Dict[str, Any]:
        """Process a single symbol: fetch data, generate signal, execute trade."""
        # Check cooldown
        now = datetime.datetime.utcnow()
        if symbol in self._symbol_cooldowns:
            cooldown_end = self._symbol_cooldowns[symbol]
            if now < cooldown_end:
                return {"symbol": symbol, "action": "cooldown", "cooldown_until": cooldown_end.isoformat()}

        # Fetch market data
        try:
            bars = self.market_data_service.fetch_bars(symbol, limit=50)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch bars for {symbol}: {exc}")

        # Generate signal
        signals = self.strategy.generate_signals(symbol, bars)
        if not signals:
            signal = {"signal": "HOLD", "price": 100.0}
        else:
            latest_signal = signals[-1]
            # Use exec_service to process the signal
            result = self.exec_service.process_signal(latest_signal)
            signal = {
                "signal": latest_signal.signal.value,
                "price": latest_signal.price,
                "strength": latest_signal.strength,
                "reason": latest_signal.reason,
            }
            # The result from exec_service will have the order if submitted
            if result.get("order"):
                self._last_order = result["order"]
                # Set cooldown
                self._symbol_cooldowns[symbol] = now + datetime.timedelta(seconds=self.settings.cooldown_seconds_per_symbol)
                return {"symbol": symbol, "signal": signal["signal"], "action": result["action"], "order": result["order"]}

        self._last_signals[symbol] = {
            "signal": signal["signal"],
            "timestamp": now.isoformat(),
        }

        # Check market open for real orders
        if self.settings.is_alpaca_mode and self.settings.trading_enabled:
            if not self.broker.is_market_open():
                return {"symbol": symbol, "signal": signal["signal"], "action": "market_closed"}

        # For now, if no order was submitted by exec_service, return hold
        return {"symbol": symbol, "signal": signal["signal"], "action": "hold"}

    def _calculate_position_size(self, symbol: str, current_price: float) -> int:
        """Calculate position size based on risk and available cash."""
        try:
            max_quantity = int(self.settings.max_position_notional // current_price)
            # Bound by buying power
            account = self.broker.get_account()
            buying_power = account.buying_power
            max_by_bp = int(buying_power // current_price)
            quantity = min(max_quantity, max_by_bp, 1000)  # Cap at 1000 for safety
            return max(quantity, 0)
        except Exception:
            return 0


# Global instance
_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    if _auto_trader is None:
        _auto_trader = AutoTrader()
    return _auto_trader