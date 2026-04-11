from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.config.settings import Settings, get_settings
from app.db.models import AutoTraderRun, BotRunHistory
from app.db.session import SessionLocal
from app.domain.models import AssetClass, AssetMetadata, NormalizedMarketSnapshot, SessionState
from app.ml.inference import SignalScorer
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.events import build_signal_id, normalize_outcome_classification
from app.monitoring.logger import get_logger
from app.news.feature_store import NewsFeatureStore
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
        self.tranche_state = runtime.tranche_state
        self.asset_catalog = runtime.asset_catalog
        self.scanner = runtime.scanner
        self.market_overview = runtime.market_overview
        self.strategy_registry = runtime.strategy_registry
        self.strategy = runtime.strategy
        self._execution_lock = runtime.lock
        self.ml_scorer = SignalScorer(self.settings)
        self.news_feature_store = NewsFeatureStore(self.settings)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        self._state_lock = threading.RLock()
        self._last_run_time: Optional[datetime.datetime] = None
        self._last_scanned_symbols: List[str] = []
        self._last_signals: Dict[str, Any] = {}
        self._last_order: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_run_result: Optional[Dict[str, Any]] = None
        self._last_accepted_candidate: Optional[Dict[str, Any]] = None
        self._last_rejected_candidate: Optional[Dict[str, Any]] = None
        self._last_symbol_evaluations: List[Dict[str, Any]] = []
        self._last_ranked_candidates: List[Dict[str, Any]] = []
        self._last_regime_snapshot: Dict[str, Any] = {}
        self._last_scan_overview: Dict[str, Any] = {}
        self._market_open: bool = True
        self._market_session_state: str | None = None
        self._cycle_guard = threading.Lock()
        self._cycle_counter: int = 0
        self._last_cycle_id: str | None = None
        self._loop_thread_ident: int | None = None
        self._summary_dedupe_ttl_seconds: float = 60.0
        self._recent_summary_fingerprints: Dict[str, float] = {}
        self._summary_dedupe_suppressed: int = 0
        self._last_notification_ids: List[str] = []
        self._latest_skipped_reason: str | None = None
        self._latest_rejected_reason: str | None = None
        self._last_submitted_order: Dict[str, Any] | None = None
        self._order_status_memory: Dict[str, str] = {}
        self._latest_broker_order_status_updates: List[Dict[str, Any]] = []
        self._broker_status_dedupe_suppressed: int = 0
        self._process_lock_handle: Any | None = None

    def start(self) -> bool:
        with self._state_lock:
            if self._running or (self._thread and self._thread.is_alive()):
                logger.info("Paper auto-trader start skipped because it is already running")
                return False
            if not self._acquire_process_lock():
                logger.warning(
                    "Paper auto-trader start blocked because another process holds the loop lock",
                    extra={"lock_path": self.settings.auto_trader_lock_path},
                )
                return False

            self._running = True
            self._loop_thread_ident = None
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="money-auto-trader",
            )
            self._thread.start()

        logger.info(
            "Paper auto-trader is running",
            extra={
                "broker_mode": self.settings.broker_mode,
                "broker_backend": self.settings.broker_backend,
                "active_strategy": self.settings.active_strategy,
                "scan_interval_seconds": self.settings.scan_interval_seconds,
                "trading_enabled": self.settings.trading_enabled,
                "auto_trade_enabled": self.settings.auto_trade_enabled,
                "discord_enabled": self.settings.discord_notifications_enabled,
                "thread_name": self._thread.name if self._thread else None,
            },
        )
        self._notify_system_event(
            event="Paper auto-trader started",
            reason="background loop started",
            details={
                "broker_mode": self.settings.broker_mode,
                "active_strategy": self.settings.active_strategy,
                "scan_interval_seconds": self.settings.scan_interval_seconds,
                "trading_enabled": self.settings.trading_enabled,
                "auto_trade_enabled": self.settings.auto_trade_enabled,
                "discord_enabled": self.settings.discord_notifications_enabled,
            },
        )
        return True

    def stop(self) -> bool:
        with self._state_lock:
            thread = self._thread
            if not self._running and not (thread and thread.is_alive()):
                logger.info("Paper auto-trader stop skipped because it is not running")
                self._thread = None
                return False
            self._running = False
            self._wake_event.set()

        if thread:
            thread.join(timeout=5.0)

        with self._state_lock:
            self._thread = None
        self._release_process_lock()

        logger.info(
            "Paper auto-trader stopped",
            extra={
                "broker_mode": self.settings.broker_mode,
                "active_strategy": self.settings.active_strategy,
                "thread_ident": self._loop_thread_ident,
                "discord_enabled": self.settings.discord_notifications_enabled,
            },
        )
        self._notify_system_event(
            event="Paper auto-trader stopped",
            reason="background loop stopped",
            details={
                "broker_mode": self.settings.broker_mode,
                "active_strategy": self.settings.active_strategy,
                "trading_enabled": self.settings.trading_enabled,
                "auto_trade_enabled": self.settings.auto_trade_enabled,
                "discord_enabled": self.settings.discord_notifications_enabled,
            },
        )
        return True

    def run_now(self) -> Dict[str, Any]:
        try:
            results = self._scan_and_trade(mode="manual_run_now")
            with self._state_lock:
                self._last_run_time = datetime.datetime.utcnow()
                self._last_error = None
                self._last_run_result = self._summarize_results("manual_run_now", results)
            return {"success": True, "results": results}
        except Exception as exc:
            error_msg = f"Run-now failed: {exc}"
            logger.error(error_msg)
            with self._state_lock:
                self._last_error = error_msg
                self._last_run_result = {
                    "mode": "manual_run_now",
                    "status": "error",
                    "error": error_msg,
                }
            self._notify_cycle_failure(exc, context={"mode": "run_now"})
            return {"success": False, "error": error_msg}

    def run_symbol_now(self, symbol: str, asset_class: AssetClass | str | None = None) -> Dict[str, Any]:
        now = datetime.datetime.utcnow()
        asset = self._resolve_asset(symbol, asset_class)
        cycle_id = self._next_cycle_id("manual_symbol")
        try:
            with self._execution_lock:
                self.tranche_state.increment_scan_bar_index()
                self._sync_portfolio_from_broker()
                signal = self._evaluate_asset(asset, prefer_primary_strategy=True, evaluation_mode="manual")
                news_features = self._load_news_features([asset.symbol]).get(asset.symbol)
                self._enrich_signal(
                    signal,
                    cycle_id=cycle_id,
                    regime_snapshot=self._last_regime_snapshot,
                    ranked_opportunity=None,
                    news_features=news_features,
                )
                signal = self._apply_ml_score_filter(
                    signal,
                    regime_snapshot=self._last_regime_snapshot,
                    news_features=news_features,
                )
                execution = self.execution_service.process_signal(signal)
                result = self._build_execution_result(asset.symbol, signal, execution)
                if execution.get("order"):
                    self._record_order(execution["order"])
                self._track_execution_result(result)
                self._last_symbol_evaluations = [self._build_symbol_evaluation(asset, signal, execution)]
                self._persist_run([result], run_type="manual_symbol")
                self._observe_broker_order_statuses(cycle_id=cycle_id)

            with self._state_lock:
                self._last_run_time = now
                self._last_error = None
                self._last_scanned_symbols = [asset.symbol]
                self._last_signals[asset.symbol] = signal.to_dict()
                self._last_cycle_id = cycle_id
                self._last_run_result = self._summarize_results("manual_symbol", [result])
            return result
        except Exception as exc:
            with self._state_lock:
                self._last_error = f"Run-once failed for {asset.symbol}: {exc}"
                self._last_run_result = {
                    "mode": "manual_symbol",
                    "status": "error",
                    "symbol": asset.symbol,
                    "error": self._last_error,
                }
            raise

    def get_status(self) -> Dict[str, Any]:
        latest_rejection = self.risk_manager.get_rejection_snapshot(limit=1)["latest"]
        notifier_diagnostics = get_discord_notifier(self.settings).diagnostics()
        with self._state_lock:
            return {
                "enabled": self.settings.auto_trade_enabled,
                "running": self._running,
                "broker_mode": self.settings.broker_mode,
                "broker_backend": self.settings.broker_backend,
                "trading_enabled": self.settings.trading_enabled,
                "active_strategy": self.settings.active_strategy,
                "scan_interval_seconds": self.settings.scan_interval_seconds,
                "last_run_time": self._last_run_time.isoformat() if self._last_run_time else None,
                "active_symbols": self.settings.active_symbols,
                "strategy_name": self.settings.active_strategy,
                "dry_run": not self.settings.trading_enabled,
                "last_scanned_symbols": self._last_scanned_symbols,
                "last_signals": self._last_signals,
                "last_order": self._last_order,
                "last_error": self._last_error,
                "last_run_result": self._last_run_result,
                "last_rejection": latest_rejection,
                "last_rejection_reason": latest_rejection.get("reason") if latest_rejection else None,
                "last_accepted_candidate": self._last_accepted_candidate,
                "last_rejected_candidate": self._last_rejected_candidate,
                "last_symbol_evaluations": self._last_symbol_evaluations,
                "tranche_state": self.tranche_state.snapshot(),
                "last_ranked_candidates": self._last_ranked_candidates,
                "last_regime_snapshot": self._last_regime_snapshot,
                "last_scan_overview": self._last_scan_overview,
                "market_open": self._market_open,
                "market_session_state": self._market_session_state,
                "crypto_monitoring_active": self.settings.crypto_trading_enabled,
                "quote_stale_after_seconds": self.settings.quote_stale_after_seconds,
                "strategy_routing": {
                    "equity": self.settings.strategy_for_asset_class(AssetClass.EQUITY),
                    "etf": self.settings.strategy_for_asset_class(AssetClass.ETF),
                    "crypto": self.settings.strategy_for_asset_class(AssetClass.CRYPTO),
                },
                "allow_extended_hours": self.settings.allow_extended_hours,
                "scan_summary_notifications_enabled": self.settings.discord_notify_scan_summary,
                "last_cycle_id": self._last_cycle_id,
                "latest_rejected_reason": self._latest_rejected_reason,
                "latest_skipped_reason": self._latest_skipped_reason,
                "last_submitted_order": self._last_submitted_order,
                "latest_broker_order_status_updates": self._latest_broker_order_status_updates,
                "broker_status_dedupe_suppressed": self._broker_status_dedupe_suppressed,
                "summary_dedupe_suppressed": self._summary_dedupe_suppressed,
                "recent_notification_ids": self._last_notification_ids,
                "notifier_diagnostics": notifier_diagnostics,
                "thread_ident": self._loop_thread_ident,
                "open_positions_count": len(self.portfolio.positions),
                "ml_enabled": self.settings.ml_enabled,
                "ml_model_type": self.settings.ml_model_type,
                "ml_min_score_threshold": self.settings.ml_min_score_threshold,
                "news_features_enabled": self.settings.news_features_enabled,
                "auto_trader_lock_path": self.settings.auto_trader_lock_path,
                "process_lock_acquired": self._process_lock_handle is not None,
            }

    def _run_loop(self) -> None:
        with self._state_lock:
            self._loop_thread_ident = threading.get_ident()
        while True:
            with self._state_lock:
                if not self._running:
                    break

            try:
                self._scan_and_trade(mode="background_loop")
                with self._state_lock:
                    self._last_run_time = datetime.datetime.utcnow()
                    self._last_error = None
            except Exception as exc:
                error_msg = f"Auto-trader cycle failed: {exc}"
                logger.error(error_msg)
                with self._state_lock:
                    self._last_error = error_msg
                    self._last_run_result = {
                        "mode": "background_loop",
                        "status": "error",
                        "error": error_msg,
                    }
                self._notify_cycle_failure(exc, context={"mode": "background_loop"})

            if self._wake_event.wait(self.settings.scan_interval_seconds):
                self._wake_event.clear()

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
        try:
            session = self.market_data_service.get_session_status(AssetClass.EQUITY)
            self._market_session_state = getattr(session.session_state, "value", str(session.session_state))
        except Exception:
            self._market_session_state = None

    def _select_strategy_for_asset(self, asset: AssetMetadata) -> Any:
        requested_name = self.settings.strategy_for_asset_class(asset.asset_class)
        try:
            strategy = self.strategy_registry.get(requested_name)
            if strategy.supports(asset.asset_class):
                return strategy
        except Exception:
            pass

        try:
            active_strategy = self.strategy_registry.get(self.settings.active_strategy)
            if active_strategy.supports(asset.asset_class):
                return active_strategy
        except Exception:
            pass

        if asset.asset_class == AssetClass.CRYPTO:
            try:
                return self.strategy_registry.get("crypto_momentum_trend")
            except Exception:
                return self.strategy
        return self.strategy

    def _build_context(
        self,
        asset: AssetMetadata,
        bars: Any,
        *,
        strategy: Any,
        snapshot: NormalizedMarketSnapshot,
    ) -> StrategyContext:
        benchmark_bars = None
        regime_symbol = getattr(strategy, "regime_symbol", "SPY")
        if asset.asset_class in {AssetClass.EQUITY, AssetClass.ETF} and asset.symbol != regime_symbol:
            try:
                # Fetch enough benchmark data for regime strategies
                regime_long_sma = getattr(strategy, "regime_long_sma", 25)
                benchmark_limit = (
                    250
                    if getattr(strategy, "name", "") == "equity_momentum_breakout"
                    else max(30, regime_long_sma + 5)
                )
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
                "normalized_snapshot": snapshot.to_dict(),
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

    def _evaluate_asset(
        self,
        asset: AssetMetadata,
        prefer_primary_strategy: bool = False,
        *,
        evaluation_mode: str = "auto",
        precomputed_snapshot: dict[str, Any] | None = None,
    ) -> TradeSignal:
        strategy = self._select_strategy_for_asset(asset)
        normalized_snapshot: NormalizedMarketSnapshot
        if precomputed_snapshot:
            normalized_snapshot = NormalizedMarketSnapshot.from_dict(precomputed_snapshot)
        else:
            normalized_snapshot = self.market_data_service.get_normalized_snapshot(asset.symbol, asset.asset_class)

        if not strategy.supports(asset.asset_class):
            return TradeSignal(
                symbol=asset.symbol,
                signal=Signal.HOLD,
                asset_class=asset.asset_class,
                strategy_name=strategy.name,
                reason=(
                    f"Selected strategy '{strategy.name}' does not support "
                    f"asset class '{asset.asset_class.value}'."
                ),
                price=normalized_snapshot.evaluation_price,
                entry_price=normalized_snapshot.evaluation_price,
                metrics={
                    "decision_code": "unsupported_asset_class",
                    "evaluation_mode": evaluation_mode,
                    "normalized_snapshot": normalized_snapshot.to_dict(),
                    "strategy_selected": strategy.name,
                    "asset_class": asset.asset_class.value,
                },
            )

        # Session eligibility check for equities/ETFs outside market hours
        if asset.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            session_status = self.market_data_service.get_session_status(asset.asset_class)
            is_regular_session = session_status.session_state in {SessionState.REGULAR.value, SessionState.REGULAR}
            if not is_regular_session and not self.settings.allow_extended_hours:
                return TradeSignal(
                    symbol=asset.symbol,
                    signal=Signal.HOLD,
                    asset_class=asset.asset_class,
                    strategy_name=strategy.name,
                    reason=(
                        "Skipped: market_closed_extended_hours_disabled "
                        f"(session={getattr(session_status.session_state, 'value', session_status.session_state)})."
                    ),
                    price=normalized_snapshot.evaluation_price,
                    entry_price=normalized_snapshot.evaluation_price,
                    metrics={
                        "decision_code": "market_closed_extended_hours_disabled",
                        "evaluation_mode": evaluation_mode,
                        "session_state": getattr(session_status.session_state, 'value', str(session_status.session_state)),
                        "is_regular_session": is_regular_session,
                        "allow_extended_hours": self.settings.allow_extended_hours,
                        "normalized_snapshot": normalized_snapshot.to_dict(),
                        "strategy_selected": strategy.name,
                        "asset_class": asset.asset_class.value,
                    },
                )

        # Fetch enough data for regime strategies (at least 250 bars for 200-day regime)
        min_bars = 250 if getattr(strategy, "name", "") == "equity_momentum_breakout" else 60
        bars = self.market_data_service.fetch_bars(
            asset.symbol,
            asset_class=asset.asset_class,
            timeframe=self.settings.default_timeframe,
            limit=min_bars,
        )
        context = self._build_context(asset, bars, strategy=strategy, snapshot=normalized_snapshot)
        candidate_signals: list[TradeSignal] = []
        strategy_input: Any = bars
        if getattr(strategy, "name", "") == "equity_momentum_breakout":
            strategy_input = {"symbol": bars, "benchmark": context.metadata.get("benchmark_bars")}
        try:
            candidate_signals.extend(strategy.generate_signals(asset.symbol, strategy_input, context=context))
        except TypeError as exc:
            if "unexpected keyword argument 'context'" not in str(exc):
                raise
            candidate_signals.extend(strategy.generate_signals(asset.symbol, strategy_input))
        except Exception as exc:
            logger.warning("Strategy evaluation failed for %s: %s", asset.symbol, exc)
        if not candidate_signals:
            return TradeSignal(
                symbol=asset.symbol,
                signal=Signal.HOLD,
                asset_class=asset.asset_class,
                strategy_name=strategy.name,
                reason=f"Selected strategy '{strategy.name}' generated no signal.",
                price=normalized_snapshot.evaluation_price,
                entry_price=normalized_snapshot.evaluation_price,
                metrics={
                    "decision_code": "no_signal",
                    "evaluation_mode": evaluation_mode,
                    "normalized_snapshot": normalized_snapshot.to_dict(),
                    "strategy_selected": strategy.name,
                    "asset_class": asset.asset_class.value,
                },
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
        if normalized_snapshot.evaluation_price is not None:
            signal.price = float(normalized_snapshot.evaluation_price)
            signal.entry_price = float(normalized_snapshot.evaluation_price)
        signal.liquidity_score = signal.liquidity_score or 0.0
        if signal.metrics is None:
            signal.metrics = {}
        latest_volume = float(bars.iloc[-1]["Volume"]) if not bars.empty else None
        signal.metrics.setdefault("avg_volume", float(bars["Volume"].tail(10).mean()) if not bars.empty else None)
        signal.metrics.setdefault("dollar_volume", (signal.metrics.get("avg_volume") or 0.0) * (signal.entry_price or signal.price or 0.0))
        signal.metrics.setdefault("latest_volume", latest_volume)
        signal.metrics["spread_pct"] = normalized_snapshot.spread_pct
        signal.metrics["decision_code"] = signal.metrics.get("decision_code") or ("no_signal" if signal.signal == Signal.HOLD else "signal")
        signal.metrics["evaluation_mode"] = evaluation_mode
        signal.metrics["strategy_selected"] = strategy.name
        signal.metrics["asset_class"] = asset.asset_class.value
        signal.metrics["normalized_snapshot"] = normalized_snapshot.to_dict()
        signal.metrics["quote_available"] = normalized_snapshot.quote_available
        signal.metrics["quote_stale"] = normalized_snapshot.quote_stale
        signal.metrics["price_source_used"] = normalized_snapshot.price_source_used
        signal.metrics["fallback_pricing_used"] = normalized_snapshot.fallback_pricing_used
        signal.metrics["quote_timestamp"] = (
            normalized_snapshot.quote_timestamp.isoformat() if normalized_snapshot.quote_timestamp else None
        )
        signal.metrics["quote_age_seconds"] = normalized_snapshot.quote_age_seconds
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
                "decision_code": "no_position_to_sell",
                "blocked_rule": "no_position_to_sell",
                "blocked_reason": hold_reason,
            },
        )

    def _scan_and_trade(self, *, mode: str = "auto") -> List[Dict[str, Any]]:
        if not self._cycle_guard.acquire(blocking=False):
            logger.info("Scan cycle skipped because another cycle is already running", extra={"mode": mode})
            with self._state_lock:
                self._last_run_result = {
                    "mode": mode,
                    "status": "skipped",
                    "reason": "cycle_in_progress",
                    "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                }
            return []

        results: List[Dict[str, Any]] = []
        now = datetime.datetime.utcnow()
        cycle_id = self._next_cycle_id(mode)
        try:
            logger.info(
                "Starting scan",
                extra={
                    "cycle_id": cycle_id,
                    "mode": mode,
                    "active_symbols": self.settings.active_symbols,
                    "strategy_name": self.settings.active_strategy,
                    "dry_run": not self.settings.trading_enabled,
                    "universe_scan_enabled": self.settings.universe_scan_enabled,
                },
            )

            with self._execution_lock:
                scan_bar_index = self.tranche_state.increment_scan_bar_index()
                self._sync_portfolio_from_broker()
                if self.settings.universe_scan_enabled:
                    scan_result = self.scanner.scan(limit=max(10, self.settings.max_positions_total * 3))
                else:
                    scan_result = self.scanner.scan(
                        symbols=self.settings.active_symbols,
                        limit=len(self.settings.active_symbols),
                    )

                candidate_symbols = [item.symbol for item in scan_result.opportunities]
                open_symbols = list(self.portfolio.positions.keys())
                all_symbols = list(dict.fromkeys(candidate_symbols + open_symbols))
                assets = [self._resolve_asset(symbol) for symbol in all_symbols]
                snapshot_by_symbol = scan_result.symbol_snapshots or {}
                opportunity_by_symbol = {item.symbol: item for item in scan_result.opportunities}
                news_features_by_symbol = self._load_news_features(all_symbols)

                signals: list[TradeSignal] = []
                symbol_evaluations: list[dict[str, Any]] = []
                for asset in assets:
                    signal = self._evaluate_asset(
                        asset,
                        evaluation_mode="auto",
                        precomputed_snapshot=snapshot_by_symbol.get(asset.symbol),
                    )
                    self._enrich_signal(
                        signal,
                        cycle_id=cycle_id,
                        regime_snapshot=scan_result.regime_status,
                        ranked_opportunity=opportunity_by_symbol.get(asset.symbol),
                        news_features=news_features_by_symbol.get(asset.symbol),
                    )
                    signal = self._apply_ml_score_filter(
                        signal,
                        regime_snapshot=scan_result.regime_status,
                        news_features=news_features_by_symbol.get(asset.symbol),
                    )
                    self._last_signals[asset.symbol] = signal.to_dict()
                    decision_code = str((signal.metrics or {}).get("decision_code") or "")
                    evaluation_action = self._classify_evaluation_action(signal)
                    symbol_evaluations.append(
                        {
                            "symbol": asset.symbol,
                            "asset_class": asset.asset_class.value,
                            "strategy_selected": (signal.metrics or {}).get("strategy_selected", signal.strategy_name),
                            "market_session_state": (signal.metrics or {}).get("normalized_snapshot", {}).get("session_state"),
                            "latest_normalized_snapshot": (signal.metrics or {}).get("normalized_snapshot"),
                            "quote_available": (signal.metrics or {}).get("quote_available"),
                            "price_source_for_ranking": (
                                (scan_result.symbol_snapshots.get(asset.symbol, {}) if scan_result.symbol_snapshots else {}).get("price_source_used")
                            ),
                            "price_source_for_signal": (signal.metrics or {}).get("price_source_used"),
                            "price_source_for_order_proposal": None,
                            "price_source_for_spread_check": None,
                            "latest_price": signal.price,
                            "signal": signal.signal.value,
                            "action": evaluation_action,
                            "classification": normalize_outcome_classification(evaluation_action, decision_code),
                            "decision_rule": decision_code or None,
                            "decision_reason": signal.reason,
                            "ml_score": ((signal.metrics or {}).get("ml") or {}).get("score"),
                            "ml_passed": ((signal.metrics or {}).get("ml") or {}).get("passed"),
                        }
                    )
                    if (
                        signal.signal != Signal.HOLD
                        or asset.symbol in self.portfolio.positions
                        or evaluation_action == "skipped"
                    ):
                        signals.append(signal)

                skipped_hold_signals = [signal for signal in signals if signal.signal == Signal.HOLD]
                sell_signals = [signal for signal in signals if signal.signal == Signal.SELL]
                buy_signals = [signal for signal in signals if signal.signal == Signal.BUY]
                buy_signals = sorted(
                    buy_signals,
                    key=lambda item: (
                        ((item.metrics or {}).get("ml") or {}).get("score") or 0.0,
                        item.confidence_score or 0.0,
                        item.momentum_score or 0.0,
                    ),
                    reverse=True,
                )
                open_symbols_set = set(self.portfolio.positions.keys())
                scale_in_buy_signals = [
                    signal
                    for signal in buy_signals
                    if signal.symbol in open_symbols_set and self.execution_service.has_pending_tranche(signal.symbol)
                ]
                new_symbol_buy_signals = [
                    signal
                    for signal in buy_signals
                    if signal.symbol not in open_symbols_set
                ]
                available_slots = max(0, self.settings.max_positions_total - len(self.portfolio.positions))
                selected_buy_signals = scale_in_buy_signals + new_symbol_buy_signals[:available_slots]

                for signal in skipped_hold_signals + sell_signals + selected_buy_signals:
                    execution = self.execution_service.process_signal(signal)
                    action_result = self._build_execution_result(signal.symbol, signal, execution)
                    results.append(action_result)
                    for row in symbol_evaluations:
                        if row["symbol"] == signal.symbol:
                            row["action"] = execution.get("action")
                            row["latest_price"] = execution.get("latest_price")
                            row["decision_rule"] = (execution.get("risk") or {}).get("rule")
                            row["decision_reason"] = (execution.get("risk") or {}).get("reason")
                            row["price_source_for_order_proposal"] = (
                                (execution.get("risk") or {}).get("details", {}).get("price_source_used")
                            )
                            row["price_source_for_spread_check"] = (
                                (execution.get("risk") or {}).get("details", {}).get("price_source_used")
                            )
                            row["classification"] = action_result.get("classification")
                            row["ml_score"] = ((signal.metrics or {}).get("ml") or {}).get("score")
                            row["ml_passed"] = ((signal.metrics or {}).get("ml") or {}).get("passed")
                            break
                    if execution.get("order"):
                        self._record_order(execution["order"])
                    self._track_execution_result(action_result)

                for row in symbol_evaluations:
                    if row.get("action") == "candidate":
                        row["action"] = "skipped"
                        row["decision_rule"] = row.get("decision_rule") or "not_selected_by_rank"
                        row["decision_reason"] = row.get("decision_reason") or "Candidate not selected this cycle."

                outcome_counts = self._count_outcomes(symbol_evaluations)
                self._observe_broker_order_statuses(cycle_id=cycle_id)

                with self._state_lock:
                    self._last_run_time = now
                    self._last_cycle_id = cycle_id
                    self._last_scanned_symbols = all_symbols
                    self._last_ranked_candidates = [item.to_dict() for item in scan_result.opportunities]
                    self._last_regime_snapshot = scan_result.regime_status
                    self._last_scan_overview = scan_result.to_dict()
                    self._last_scan_overview["scan_bar_index"] = scan_bar_index
                    self._last_scan_overview["cycle_id"] = cycle_id
                    self._last_scan_overview["mode"] = mode
                    self._last_scan_overview["outcome_counts"] = outcome_counts
                    self._last_symbol_evaluations = symbol_evaluations
                    self._last_run_result = self._summarize_results(mode, results)

                run_type = "manual" if mode == "manual_run_now" else "auto"
                self._persist_run(results, run_type=run_type)
                self._notify_scan_summary(
                    cycle_id=cycle_id,
                    all_symbols=all_symbols,
                    evaluations=symbol_evaluations,
                    results=results,
                    outcome_counts=outcome_counts,
                )
                return results
        finally:
            self._cycle_guard.release()

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
            "classification": normalize_outcome_classification(
                str(execution.get("action") or ""),
                (execution.get("risk") or {}).get("rule") if isinstance(execution.get("risk"), dict) else None,
            ),
        }

    def _build_symbol_evaluation(
        self,
        asset: AssetMetadata,
        signal: TradeSignal,
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        snapshot = (signal.metrics or {}).get("normalized_snapshot", {})
        return {
            "symbol": asset.symbol,
            "asset_class": asset.asset_class.value,
            "strategy_selected": (signal.metrics or {}).get("strategy_selected", signal.strategy_name),
            "market_session_state": snapshot.get("session_state"),
            "latest_normalized_snapshot": snapshot,
            "quote_available": snapshot.get("quote_available"),
            "price_source_for_ranking": snapshot.get("price_source_used"),
            "price_source_for_signal": snapshot.get("price_source_used"),
            "price_source_for_order_proposal": (execution.get("risk") or {}).get("details", {}).get("price_source_used"),
            "price_source_for_spread_check": (execution.get("risk") or {}).get("details", {}).get("price_source_used"),
            "latest_price": execution.get("latest_price"),
            "signal": signal.signal.value,
            "action": execution.get("action"),
            "classification": normalize_outcome_classification(
                str(execution.get("action") or ""),
                (execution.get("risk") or {}).get("rule") if isinstance(execution.get("risk"), dict) else None,
            ),
            "decision_rule": (execution.get("risk") or {}).get("rule"),
            "decision_reason": (execution.get("risk") or {}).get("reason"),
            "ml_score": ((signal.metrics or {}).get("ml") or {}).get("score"),
            "ml_passed": ((signal.metrics or {}).get("ml") or {}).get("passed"),
        }

    def _record_order(self, order: Dict[str, Any]) -> None:
        with self._state_lock:
            self._last_order = order
            self._last_submitted_order = order

    def _track_execution_result(self, result: Dict[str, Any]) -> None:
        with self._state_lock:
            action = str(result.get("action") or "")
            risk = result.get("risk") or {}
            if action == "rejected":
                self._last_rejected_candidate = result
                self._latest_rejected_reason = risk.get("reason")
            elif action in {"submitted", "dry_run"}:
                self._last_accepted_candidate = result
                self._last_submitted_order = result.get("order")
            elif action == "skipped":
                self._latest_skipped_reason = risk.get("reason")

    def _next_cycle_id(self, mode: str) -> str:
        with self._state_lock:
            self._cycle_counter += 1
            sequence = self._cycle_counter
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        compact_mode = mode.replace(" ", "_")
        return f"{compact_mode}-{stamp}-{sequence:06d}"

    def _classify_evaluation_action(self, signal: TradeSignal) -> str:
        if signal.signal != Signal.HOLD:
            return "candidate"
        decision_code = str((signal.metrics or {}).get("decision_code") or "")
        if decision_code in {
            "market_closed",
            "market_closed_extended_hours_disabled",
            "extended_hours_not_supported_for_asset",
            "no_position_to_sell",
            "skipped_low_ml_score",
            "ml_inference_error",
        }:
            return "skipped"
        return "hold"

    def _count_outcomes(
        self,
        evaluations: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {"submitted": 0, "rejected": 0, "skipped": 0, "hold": 0}

        for row in evaluations:
            action = str(row.get("action") or "").lower()
            if action == "candidate":
                action = "skipped"
            if action == "dry_run":
                counts["submitted"] += 1
                continue
            if action in counts:
                counts[action] += 1
            else:
                counts["hold"] += 1
        return counts

    def _summary_fingerprint(self, evaluations: list[dict[str, Any]]) -> str:
        compact = [
            {
                "symbol": str(item.get("symbol") or ""),
                "action": str(item.get("action") or ""),
                "rule": str(item.get("decision_rule") or ""),
            }
            for item in evaluations
        ]
        raw = json.dumps(compact, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _should_emit_summary(self, fingerprint: str) -> bool:
        now = time.monotonic()
        with self._state_lock:
            expired = [
                key
                for key, seen_at in self._recent_summary_fingerprints.items()
                if now - seen_at > self._summary_dedupe_ttl_seconds
            ]
            for key in expired:
                self._recent_summary_fingerprints.pop(key, None)

            seen_at = self._recent_summary_fingerprints.get(fingerprint)
            if seen_at is not None and now - seen_at <= self._summary_dedupe_ttl_seconds:
                self._summary_dedupe_suppressed += 1
                return False
            self._recent_summary_fingerprints[fingerprint] = now
            return True

    def _remember_notification_id(self, notification_id: str) -> None:
        with self._state_lock:
            self._last_notification_ids.append(notification_id)
            self._last_notification_ids = self._last_notification_ids[-20:]

    def _normalize_broker_status(self, status: str | None) -> str | None:
        if status is None:
            return None
        normalized = str(status).strip().lower()
        mappings = {
            "new": "accepted",
            "accepted": "accepted",
            "pending_new": "accepted",
            "pending_replace": "accepted",
            "accepted_for_bidding": "accepted",
            "partially_filled": "partially_filled",
            "filled": "filled",
            "canceled": "canceled",
            "expired": "canceled",
            "done_for_day": "canceled",
            "rejected": "rejected",
            "suspended": "rejected",
        }
        return mappings.get(normalized)

    def _observe_broker_order_statuses(self, *, cycle_id: str) -> None:
        try:
            orders = self.broker.list_orders()
        except Exception as exc:
            logger.warning("Failed to poll broker order statuses: %s", exc)
            return

        notifier = get_discord_notifier(self.settings)
        updates: list[dict[str, Any]] = []
        for order in orders:
            order_id = str(order.get("id") or order.get("client_order_id") or "")
            if not order_id:
                continue
            status = self._normalize_broker_status(str(order.get("status") or ""))
            if status is None:
                continue

            with self._state_lock:
                previous = self._order_status_memory.get(order_id)
                if previous == status:
                    self._broker_status_dedupe_suppressed += 1
                    continue
                self._order_status_memory[order_id] = status

            updates.append(
                {
                    "cycle_id": cycle_id,
                    "order_id": order_id,
                    "symbol": order.get("symbol"),
                    "status": status,
                    "raw_status": order.get("status"),
                    "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                }
            )
            if notifier.send_broker_lifecycle_notification(
                status=status,
                order=order,
                strategy_name=self.settings.active_strategy,
            ):
                self._remember_notification_id(f"broker:{order_id}:{status}")

        if updates:
            with self._state_lock:
                self._latest_broker_order_status_updates.extend(updates)
                self._latest_broker_order_status_updates = self._latest_broker_order_status_updates[-30:]

    def _summarize_results(self, mode: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        action_counts: Dict[str, int] = {}
        for item in results:
            action = str(item.get("action", "unknown"))
            action_counts[action] = action_counts.get(action, 0) + 1
        return {
            "mode": mode,
            "status": "success",
            "cycle_id": self._last_cycle_id,
            "results_count": len(results),
            "action_counts": action_counts,
            "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }

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
        sent = notifier.send_system_notification(
            event=event,
            reason=reason,
            details=details,
            category="start_stop",
        )
        if sent:
            self._remember_notification_id(f"system:{event}:{reason}")

    def _notify_cycle_failure(self, error: Exception, context: dict[str, Any] | None = None) -> None:
        notifier = get_discord_notifier(self.settings)
        sent = notifier.send_error_notification(
            title="Auto-Trader Cycle Failed",
            message="A trading cycle failed, but the service kept running.",
            error=error,
            context={
                "broker_mode": self.settings.broker_mode,
                "trading_enabled": self.settings.trading_enabled,
                **(context or {}),
            },
        )
        if sent:
            self._remember_notification_id("error:auto_trader_cycle_failed")

    def _notify_scan_summary(
        self,
        *,
        cycle_id: str,
        all_symbols: list[str],
        evaluations: list[dict[str, Any]],
        results: list[dict[str, Any]],
        outcome_counts: dict[str, int],
    ) -> None:
        _ = results
        fingerprint = self._summary_fingerprint(evaluations)
        if not self._should_emit_summary(fingerprint):
            logger.info(
                "Scan summary notification suppressed by dedupe",
                extra={"cycle_id": cycle_id, "fingerprint": fingerprint},
            )
            return

        notifier = get_discord_notifier(self.settings)
        interesting = [
            item
            for item in evaluations
            if str(item.get("action") or "").lower() in {"submitted", "dry_run", "rejected", "skipped"}
        ]
        highlights = interesting[:5] if interesting else evaluations[:3]
        sent = notifier.send_scan_summary_notification(
            cycle_id=cycle_id,
            symbols_evaluated=len(all_symbols),
            outcome_counts=outcome_counts,
            highlights=highlights,
            timestamp=datetime.datetime.utcnow(),
        )
        if sent:
            self._remember_notification_id(f"scan_summary:{cycle_id}")

    def _load_news_features(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not self.settings.news_features_enabled:
            return {}
        try:
            return self.news_feature_store.latest_for_symbols(symbols)
        except Exception as exc:
            logger.warning("Failed to load news features: %s", exc)
            return {}

    def _enrich_signal(
        self,
        signal: TradeSignal,
        *,
        cycle_id: str,
        regime_snapshot: dict[str, Any] | None = None,
        ranked_opportunity: Any | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> None:
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics.setdefault("signal_id", build_signal_id(signal.symbol, signal.strategy_name, signal.generated_at))
        signal.metrics["cycle_id"] = cycle_id
        signal.metrics["market_overview"] = dict(regime_snapshot or {})
        if ranked_opportunity is not None:
            signal.metrics["scan_signal_quality_score"] = getattr(ranked_opportunity, "signal_quality_score", None)
            signal.metrics["scan_tags"] = list(getattr(ranked_opportunity, "tags", []))
        if news_features:
            signal.metrics["news_features"] = dict(news_features)

    def _apply_ml_score_filter(
        self,
        signal: TradeSignal,
        *,
        regime_snapshot: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> TradeSignal:
        result = self.ml_scorer.score_signal(
            signal,
            market_overview=regime_snapshot,
            news_features=news_features,
            latest_price=signal.price or signal.entry_price,
        )
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics["ml"] = result.to_dict()
        if signal.signal != Signal.BUY or result.passed:
            return signal
        if result.reason == "ml_inference_error":
            skip_reason = "Skipped: ml_inference_error (candidate could not be scored safely)."
            return TradeSignal(
                symbol=signal.symbol,
                signal=Signal.HOLD,
                asset_class=signal.asset_class,
                strategy_name=signal.strategy_name,
                signal_type=signal.signal_type,
                confidence_score=signal.confidence_score,
                price=signal.price,
                entry_price=signal.entry_price,
                reason=skip_reason,
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
                    **(signal.metrics or {}),
                    "decision_code": "ml_inference_error",
                    "original_signal": Signal.BUY.value,
                },
            )
        return TradeSignal(
            symbol=signal.symbol,
            signal=Signal.HOLD,
            asset_class=signal.asset_class,
            strategy_name=signal.strategy_name,
            signal_type=signal.signal_type,
            confidence_score=signal.confidence_score,
            price=signal.price,
            entry_price=signal.entry_price,
            reason=(
                f"Skipped: skipped_low_ml_score "
                f"(score={(result.score or 0.0):.3f}, threshold={self.settings.ml_min_score_threshold:.3f})."
            ),
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
                **(signal.metrics or {}),
                "decision_code": "skipped_low_ml_score",
                "original_signal": Signal.BUY.value,
            },
        )

    def _acquire_process_lock(self) -> bool:
        if self._process_lock_handle is not None:
            return True
        lock_path = Path(self.settings.auto_trader_lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate(0)
        handle.write(str(os.getpid()))
        handle.flush()
        self._process_lock_handle = handle
        return True

    def _release_process_lock(self) -> None:
        handle = self._process_lock_handle
        if handle is None:
            return
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        finally:
            self._process_lock_handle = None

    def reset_runtime_state(self) -> None:
        with self._state_lock:
            self._last_run_time = None
            self._last_scanned_symbols = []
            self._last_signals = {}
            self._last_order = None
            self._last_error = None
            self._last_run_result = None
            self._last_accepted_candidate = None
            self._last_rejected_candidate = None
            self._last_symbol_evaluations = []
            self._last_ranked_candidates = []
            self._last_regime_snapshot = {}
            self._last_scan_overview = {}
            self._market_open = True
            self._market_session_state = None
            self._last_cycle_id = None
            self._latest_skipped_reason = None
            self._latest_rejected_reason = None
            self._last_submitted_order = None
            self._order_status_memory = {}
            self._latest_broker_order_status_updates = []
            self._summary_dedupe_suppressed = 0
            self._broker_status_dedupe_suppressed = 0
            self._recent_summary_fingerprints = {}
            self._last_notification_ids = []
        self._release_process_lock()


_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    from app.services.runtime import get_runtime

    _auto_trader = get_runtime().get_auto_trader()
    return _auto_trader
