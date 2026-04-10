from __future__ import annotations

import datetime
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.config.settings import Settings, get_settings
from app.db.models import AutoTraderRun, BotRunHistory
from app.db.session import SessionLocal
from app.domain.models import AssetClass, AssetMetadata
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.logger import get_logger
from app.services.market_data import infer_asset_class, normalize_asset_class
from app.strategies.base import Signal, StrategyContext, TradeSignal

if TYPE_CHECKING:
    from app.services.runtime import RuntimeContainer

logger = get_logger("auto_trader")


class AutoTrader:
    """Automated trading service for periodic scanning and order execution."""

    def __init__(self, settings: Settings | None = None, runtime: RuntimeContainer | None = None):
        self.settings = settings or get_settings()
        self.runtime = runtime

        if runtime is None:
            from app.services.runtime import get_runtime

            runtime = get_runtime(self.settings)

        self.runtime = runtime
        self.broker = runtime.broker
        self.portfolio = runtime.portfolio
        self.risk_manager = runtime.risk_manager
        self.market_data_service = runtime.market_data_service
        self.execution_service = runtime.execution_service
        self.asset_catalog = runtime.asset_catalog
        self.scanner = runtime.scanner
        self.market_overview = runtime.market_overview
        self.strategy_registry = runtime.strategy_registry
        self.strategy = runtime.strategy
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
        self._last_scan_overview: Dict[str, Any] = {}
        self._market_open: bool = True

    def start(self) -> bool:
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
        self._notify_system_event(
            event="Bot started",
            reason="background loop started",
        )
        return True

    def stop(self) -> bool:
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
        self._notify_system_event(
            event="Bot stopped",
            reason="background loop stopped",
        )
        return True

    def run_now(self) -> Dict[str, Any]:
        try:
            results = self._scan_and_trade()
            with self._state_lock:
                self._last_run_time = datetime.datetime.utcnow()
                self._last_error = None
            return {"success": True, "results": results}
        except Exception as exc:
            error_msg = f"Run-now failed: {exc}"
            logger.error(error_msg)
            with self._state_lock:
                self._last_error = error_msg
            self._notify_cycle_failure(exc, context={"mode": "run_now"})
            return {"success": False, "error": error_msg}

    def run_symbol_now(self, symbol: str, asset_class: AssetClass | str | None = None) -> Dict[str, Any]:
        now = datetime.datetime.utcnow()
        asset = self._resolve_asset(symbol, asset_class)
        try:
            with self._execution_lock:
                self._sync_portfolio_from_broker()
                signal = self._evaluate_asset(asset, prefer_primary_strategy=True)
                execution = self.execution_service.process_signal(signal)
                result = self._build_execution_result(asset.symbol, signal, execution)
                if execution.get("order"):
                    self._record_order(execution["order"])
                self._persist_run([result], run_type="manual_symbol")

            with self._state_lock:
                self._last_run_time = now
                self._last_error = None
                self._last_scanned_symbols = [asset.symbol]
                self._last_signals[asset.symbol] = signal.to_dict()
            return result
        except Exception as exc:
            with self._state_lock:
                self._last_error = f"Run-once failed for {asset.symbol}: {exc}"
            raise

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "running": self._running,
                "last_run_time": self._last_run_time.isoformat() if self._last_run_time else None,
                "active_symbols": self.settings.active_symbols,
                "strategy_name": self.settings.strategy_name,
                "dry_run": not self.settings.trading_enabled,
                "last_scanned_symbols": self._last_scanned_symbols,
                "last_signals": self._last_signals,
                "last_order": self._last_order,
                "last_error": self._last_error,
                "last_ranked_candidates": self._last_ranked_candidates,
                "last_regime_snapshot": self._last_regime_snapshot,
                "last_scan_overview": self._last_scan_overview,
                "market_open": self._market_open,
                "broker_mode": self.settings.broker_mode,
                "trading_enabled": self.settings.trading_enabled,
                "open_positions_count": len(self.portfolio.positions),
            }

    def _run_loop(self) -> None:
        while True:
            with self._state_lock:
                if not self._running:
                    break

            try:
                self._scan_and_trade()
                with self._state_lock:
                    self._last_run_time = datetime.datetime.utcnow()
                    self._last_error = None
            except Exception as exc:
                error_msg = f"Auto-trader cycle failed: {exc}"
                logger.error(error_msg)
                with self._state_lock:
                    self._last_error = error_msg
                self._notify_cycle_failure(exc, context={"mode": "background_loop"})

            time.sleep(self.settings.scan_interval_seconds)

    def _resolve_asset(
        self,
        symbol: str,
        asset_class: AssetClass | str | None = None,
    ) -> AssetMetadata:
        asset = self.asset_catalog.get_asset(symbol)
        if asset is not None:
            return asset
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        broker_asset = self.broker.get_asset(symbol, resolved_asset_class)
        if broker_asset is not None:
            return broker_asset
        return AssetMetadata(
            symbol=symbol.strip().upper(),
            name=symbol.strip().upper(),
            asset_class=resolved_asset_class,
            exchange="UNKNOWN",
            tradable=True,
        )

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

        try:
            self.asset_catalog.ensure_fresh()
        except Exception as exc:
            logger.warning("Failed to refresh asset catalog: %s", exc)

        try:
            self._market_open = self.broker.is_market_open(AssetClass.EQUITY)
        except Exception:
            self._market_open = True

    def _build_context(self, asset: AssetMetadata, bars: Any) -> StrategyContext:
        benchmark_bars = None
        regime_symbol = getattr(self.strategy, "regime_symbol", "SPY")
        if asset.asset_class in {AssetClass.EQUITY, AssetClass.ETF} and asset.symbol != regime_symbol:
            try:
                # Fetch enough benchmark data for regime strategies
                regime_long_sma = getattr(self.strategy, "regime_long_sma", 25)
                benchmark_limit = 250 if self.settings.strategy_name == "regime_momentum_breakout" else max(30, regime_long_sma + 5)
                benchmark_bars = self.market_data_service.fetch_bars(
                    regime_symbol,
                    asset_class=AssetClass.ETF,
                    timeframe=self.settings.default_timeframe,
                    limit=benchmark_limit,
                )
            except Exception as exc:
                logger.warning("Failed to fetch benchmark bars: %s", exc)
        tracked_position = self.portfolio.get_position(asset.symbol)
        return StrategyContext(
            asset=asset,
            session=self.market_data_service.get_session_status(asset.asset_class),
            quote=self.market_data_service.get_latest_quote(asset.symbol, asset.asset_class),
            timeframe=self.settings.default_timeframe,
            metadata={
                "benchmark_bars": benchmark_bars,
                "short_selling_enabled": self.settings.short_selling_enabled,
                "has_tracked_position": tracked_position is not None,
                "has_sellable_long_position": self.portfolio.is_sellable_long_position(asset.symbol),
                "tracked_position": (
                    {
                        "symbol": tracked_position.symbol,
                        "quantity": tracked_position.quantity,
                        "side": tracked_position.side,
                        "entry_price": tracked_position.entry_price,
                        "current_price": tracked_position.current_price,
                        "asset_class": tracked_position.asset_class.value,
                        "exchange": tracked_position.exchange,
                    }
                    if tracked_position is not None
                    else None
                ),
            },
        )

    def _evaluate_asset(self, asset: AssetMetadata, prefer_primary_strategy: bool = False) -> TradeSignal:
        # Fetch enough data for regime strategies (at least 250 bars for 200-day regime)
        min_bars = 250 if self.settings.strategy_name == "regime_momentum_breakout" else 60
        bars = self.market_data_service.fetch_bars(
            asset.symbol,
            asset_class=asset.asset_class,
            timeframe=self.settings.default_timeframe,
            limit=min_bars,
        )
        context = self._build_context(asset, bars)
        legacy_signals: list[TradeSignal] = []
        strategy_input: Any = {"symbol": bars, "benchmark": context.metadata.get("benchmark_bars")}
        if getattr(self.strategy, "name", "") == "ema_crossover":
            strategy_input = bars
        if asset.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            try:
                legacy_signals.extend(self.strategy.generate_signals(asset.symbol, strategy_input, context=context))
            except TypeError as exc:
                if "unexpected keyword argument 'context'" not in str(exc):
                    raise
                legacy_signals.extend(self.strategy.generate_signals(asset.symbol, strategy_input))
            except Exception as exc:
                logger.warning("Legacy strategy evaluation failed for %s: %s", asset.symbol, exc)
        if prefer_primary_strategy and legacy_signals:
            candidate_signals = legacy_signals
        else:
            candidate_signals = legacy_signals + self.strategy_registry.generate_signals(asset, bars, context)
        if not candidate_signals:
            return TradeSignal(
                symbol=asset.symbol,
                signal=Signal.HOLD,
                asset_class=asset.asset_class,
                strategy_name="none",
                reason="No strategy generated a signal.",
            )
        signal = sorted(
            candidate_signals,
            key=lambda item: (
                item.signal != Signal.HOLD,
                item.confidence_score or 0.0,
                item.momentum_score or 0.0,
            ),
            reverse=True,
        )[0]
        signal = self._normalize_signal_for_long_only(asset, signal)
        signal.liquidity_score = signal.liquidity_score or 0.0
        if signal.metrics is None:
            signal.metrics = {}
        latest_volume = float(bars.iloc[-1]["Volume"]) if not bars.empty else None
        signal.metrics.setdefault("avg_volume", float(bars["Volume"].tail(10).mean()) if not bars.empty else None)
        signal.metrics.setdefault("dollar_volume", (signal.metrics.get("avg_volume") or 0.0) * (signal.entry_price or signal.price or 0.0))
        signal.metrics.setdefault("latest_volume", latest_volume)
        if context.quote is not None:
            signal.metrics.setdefault("spread_pct", context.quote.spread_pct)
        signal.metrics.setdefault("exchange", asset.exchange)
        return signal

    def _normalize_signal_for_long_only(self, asset: AssetMetadata, signal: TradeSignal) -> TradeSignal:
        if signal.signal != Signal.SELL:
            return signal

        has_tracked_position = self.portfolio.get_position(asset.symbol) is not None
        has_sellable_long_position = self.portfolio.is_sellable_long_position(asset.symbol)
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics.setdefault("has_tracked_position", has_tracked_position)
        signal.metrics.setdefault("has_tracked_long_position", has_sellable_long_position)
        signal.metrics.setdefault("short_selling_enabled", self.settings.short_selling_enabled)
        signal.metrics["is_risk_reducing_sell"] = has_sellable_long_position

        if has_sellable_long_position:
            signal.signal_type = "exit"
            return signal

        if self.settings.short_selling_enabled:
            return signal

        hold_reason = "Exit-only sell ignored: no tracked long position and short selling is disabled."
        return TradeSignal(
            symbol=signal.symbol,
            signal=Signal.HOLD,
            asset_class=signal.asset_class,
            strategy_name=signal.strategy_name,
            signal_type="exit",
            confidence_score=0.0,
            price=signal.price,
            entry_price=signal.entry_price,
            reason=f"{signal.reason} {hold_reason}".strip() if signal.reason else hold_reason,
            timestamp=signal.timestamp,
            atr=signal.atr,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            position_size=signal.position_size,
            trailing_stop=signal.trailing_stop,
            momentum_score=signal.momentum_score,
            liquidity_score=signal.liquidity_score,
            spread_score=signal.spread_score,
            regime_state=signal.regime_state,
            generated_at=signal.generated_at,
            metrics={
                **signal.metrics,
                "original_signal": signal.signal.value,
                "blocked_rule": "no_position_to_sell",
                "blocked_reason": hold_reason,
            },
        )

    def _scan_and_trade(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        now = datetime.datetime.utcnow()

        logger.info(
            "Starting scan",
            extra={
                "active_symbols": self.settings.active_symbols,
                "strategy_name": self.settings.strategy_name,
                "dry_run": not self.settings.trading_enabled,
                "universe_scan_enabled": self.settings.universe_scan_enabled,
            }
        )

        with self._execution_lock:
            self._sync_portfolio_from_broker()
            if self.settings.universe_scan_enabled:
                scan_result = self.scanner.scan(limit=max(10, self.settings.max_positions_total * 3))
            else:
                scan_result = self.scanner.scan(symbols=self.settings.active_symbols, limit=len(self.settings.active_symbols))

            candidate_symbols = [item.symbol for item in scan_result.opportunities]
            open_symbols = list(self.portfolio.positions.keys())
            all_symbols = list(dict.fromkeys(candidate_symbols + open_symbols))
            assets = [self._resolve_asset(symbol) for symbol in all_symbols]

            signals: list[TradeSignal] = []
            for asset in assets:
                signal = self._evaluate_asset(asset)
                self._last_signals[asset.symbol] = signal.to_dict()
                if signal.signal != Signal.HOLD or asset.symbol in self.portfolio.positions:
                    signals.append(signal)

            sell_signals = [signal for signal in signals if signal.signal == Signal.SELL]
            buy_signals = [signal for signal in signals if signal.signal == Signal.BUY]
            buy_signals = sorted(
                buy_signals,
                key=lambda item: (
                    item.confidence_score or 0.0,
                    item.momentum_score or 0.0,
                ),
                reverse=True,
            )
            available_slots = max(0, self.settings.max_positions_total - len(self.portfolio.positions))
            selected_buy_signals = buy_signals[:available_slots]

            for signal in sell_signals + selected_buy_signals:
                execution = self.execution_service.process_signal(signal)
                action_result = self._build_execution_result(signal.symbol, signal, execution)
                results.append(action_result)
                if execution.get("order"):
                    self._record_order(execution["order"])

            with self._state_lock:
                self._last_run_time = now
                self._last_scanned_symbols = all_symbols
                self._last_ranked_candidates = [item.to_dict() for item in scan_result.opportunities]
                self._last_regime_snapshot = scan_result.regime_status
                self._last_scan_overview = scan_result.to_dict()

            self._persist_run(results, run_type="auto")
            return results

    def _build_execution_result(
        self,
        symbol: str,
        signal: TradeSignal,
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "asset_class": signal.asset_class.value,
            "strategy_name": signal.strategy_name,
            "signal": signal.signal.value,
            "latest_price": execution.get("latest_price"),
            "proposal": execution.get("proposal", {}),
            "risk": execution.get("risk"),
            "action": execution.get("action"),
            "order": execution.get("order"),
        }

    def _record_order(self, order: Dict[str, Any]) -> None:
        with self._state_lock:
            self._last_order = order

    def _persist_run(self, results: List[Dict[str, Any]], run_type: str) -> None:
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
                        error_message=self._last_error,
                    )
                )
                session.add(
                    BotRunHistory(
                        started_at=self._last_run_time or datetime.datetime.utcnow(),
                        completed_at=datetime.datetime.utcnow(),
                        run_type=run_type,
                        status="success" if not self._last_error else "error",
                        summary_json=json.dumps(
                            {
                                "signals": self._last_signals,
                                "orders": [result.get("order") for result in results if result.get("order")],
                                "scan_overview": self._last_scan_overview,
                            },
                            default=str,
                        ),
                        error_message=self._last_error,
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist auto-trader run record: %s", exc)

    def _notify_system_event(
        self,
        *,
        event: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        notifier = get_discord_notifier(self.settings)
        notifier.send_system_notification(
            event=event,
            reason=reason,
            details=details,
            category="start_stop",
        )

    def _notify_cycle_failure(self, error: Exception, context: dict[str, Any] | None = None) -> None:
        notifier = get_discord_notifier(self.settings)
        notifier.send_error_notification(
            title="Auto-Trader Cycle Failed",
            message="A trading cycle failed, but the service kept running.",
            error=error,
            context={
                "broker_mode": self.settings.broker_mode,
                "trading_enabled": self.settings.trading_enabled,
                **(context or {}),
            },
        )

    def reset_runtime_state(self) -> None:
        with self._state_lock:
            self._last_run_time = None
            self._last_scanned_symbols = []
            self._last_signals = {}
            self._last_order = None
            self._last_error = None
            self._last_ranked_candidates = []
            self._last_regime_snapshot = {}
            self._last_scan_overview = {}
            self._market_open = True


_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    from app.services.runtime import get_runtime

    _auto_trader = get_runtime().get_auto_trader()
    return _auto_trader
