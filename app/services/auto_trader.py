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
from app.domain.models import AssetClass, AssetMetadata, NormalizedMarketSnapshot, SessionState, SignalDirection
from app.ml.inference import SignalScorer
from app.monitoring.discord_notifier import get_discord_notifier
from app.monitoring.events import build_signal_id, normalize_outcome_classification
from app.monitoring.logger import get_logger
from app.news.feature_store import NewsFeatureStore
from app.services.exit_manager import ExitManager
from app.services.market_data import infer_asset_class, normalize_asset_class
from app.services.scanner import ScanResult
from app.strategies.base import Signal, StrategyContext, TradeSignal
from app.utils.datetime_parser import parse_iso_datetime

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
        self.exit_manager = ExitManager(self.portfolio, settings=self.settings)
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
        self._latest_dust_resolution: Dict[str, Any] | None = None
        self._last_submitted_order: Dict[str, Any] | None = None
        self._process_started_at = datetime.datetime.now(datetime.timezone.utc)
        self._session_order_ids: set[str] = set()
        self._broker_status_baseline_synced: bool = False
        self._order_status_memory: Dict[str, Dict[str, Any]] = self._load_broker_order_status_memory()
        self._latest_broker_order_status_updates: List[Dict[str, Any]] = []
        self._broker_status_dedupe_suppressed: int = 0
        self._process_lock_handle: Any | None = None
        self._last_asset_class_run_at: Dict[str, datetime.datetime] = {}
        self._last_cadence_diagnostics: Dict[str, Any] = {}

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

            self._process_started_at = datetime.datetime.now(datetime.timezone.utc)
            self._session_order_ids.clear()
            self._broker_status_baseline_synced = False
            self._order_status_memory = self._load_broker_order_status_memory()
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
                "primary_runtime_strategy": self.settings.primary_runtime_strategy,
                "crypto_only_mode": self.settings.crypto_only_mode,
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
                scan_bar_index = self.tranche_state.increment_scan_bar_index()
                self._sync_portfolio_from_broker()
                normalized_snapshot = self._get_normalized_snapshot(asset)
                news_features = self._load_news_features([asset.symbol]).get(asset.symbol)
                signal = self._evaluate_exit_signal(
                    asset,
                    normalized_snapshot=normalized_snapshot,
                    evaluation_mode="manual",
                    regime_snapshot=self._last_regime_snapshot,
                    news_features=news_features,
                )
                if signal is None:
                    signal = self._evaluate_asset(
                        asset,
                        prefer_primary_strategy=True,
                        evaluation_mode="manual",
                        precomputed_snapshot=normalized_snapshot.to_dict(),
                    )
                self._enrich_signal(
                    signal,
                    cycle_id=cycle_id,
                    scan_bar_index=scan_bar_index,
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
                self._last_signals = {asset.symbol: signal.to_dict()}
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
            scan_selection_mode = self._last_scan_overview.get("scan_selection_mode")
            if scan_selection_mode is None:
                scan_selection_mode = (
                    "configured_active_symbols"
                    if self.settings.crypto_only_mode or not self.settings.universe_scan_enabled
                    else "catalog_universe"
                )
            scan_requested_symbols = self._last_scan_overview.get("scan_requested_symbols")
            if not scan_requested_symbols and scan_selection_mode == "configured_active_symbols":
                scan_requested_symbols = list(self.settings.active_symbols)
            scan_ranking_limit = self._last_scan_overview.get("scan_ranking_limit")
            if scan_ranking_limit is None:
                scan_ranking_limit = (
                    max(1, len(self.settings.active_symbols))
                    if scan_selection_mode == "configured_active_symbols"
                    else max(10, self.settings.max_positions_total * 3)
                )
            return {
                "enabled": self.settings.auto_trade_enabled,
                "running": self._running,
                "broker_mode": self.settings.broker_mode,
                "broker_backend": self.settings.broker_backend,
                "trading_enabled": self.settings.trading_enabled,
                "active_strategy": self.settings.active_strategy,
                "primary_runtime_strategy": self.settings.primary_runtime_strategy,
                "scan_interval_seconds": self.settings.scan_interval_seconds,
                "last_run_time": self._last_run_time.isoformat() if self._last_run_time else None,
                "active_symbols": self.settings.active_symbols,
                "active_crypto_symbols": self.settings.active_crypto_symbols,
                "active_asset_classes": self.settings.active_asset_classes,
                "crypto_only_mode": self.settings.crypto_only_mode,
                "primary_runtime_asset_class": self.settings.primary_runtime_asset_class.value,
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
                "scan_selection_mode": scan_selection_mode,
                "scan_requested_symbols": scan_requested_symbols or [],
                "scan_ranking_limit": scan_ranking_limit,
                "scan_selection_mode_by_asset_class": self._last_scan_overview.get("scan_selection_mode_by_asset_class", {}),
                "scan_requested_symbols_by_asset_class": self._last_scan_overview.get("scan_requested_symbols_by_asset_class", {}),
                "scan_timeframes_by_asset_class": self._last_scan_overview.get("timeframes_by_asset_class", {}),
                "scan_prefilter_counts": self._last_scan_overview.get("prefilter_counts", {}),
                "scan_final_evaluation_counts": self._last_scan_overview.get("final_evaluation_counts", {}),
                "signal_funnel": self._last_scan_overview.get("signal_funnel", {}),
                "last_scan_scanned_count": self._last_scan_overview.get("scanned_count"),
                "last_ranked_candidate_count": len(self._last_ranked_candidates),
                "last_symbol_evaluation_count": len(self._last_symbol_evaluations),
                "market_open": self._market_open,
                "market_session_state": self._market_session_state,
                "crypto_monitoring_active": AssetClass.CRYPTO in self.settings.enabled_asset_class_set,
                "quote_stale_after_seconds": self.settings.quote_stale_after_seconds,
                "strategy_routing": {
                    asset_class.value: self.settings.strategy_for_asset_class(asset_class)
                    for asset_class in sorted(self.settings.enabled_asset_class_set, key=lambda item: item.value)
                },
                "allow_extended_hours": self.settings.allow_extended_hours,
                "scan_summary_notifications_enabled": self.settings.discord_notify_scan_summary,
                "last_cycle_id": self._last_cycle_id,
                "latest_rejected_reason": self._latest_rejected_reason,
                "latest_skipped_reason": self._latest_skipped_reason,
                "latest_dust_resolution": self._latest_dust_resolution,
                "last_submitted_order": self._last_submitted_order,
                "latest_broker_order_status_updates": self._latest_broker_order_status_updates,
                "broker_status_dedupe_suppressed": self._broker_status_dedupe_suppressed,
                "summary_dedupe_suppressed": self._summary_dedupe_suppressed,
                "last_cadence_diagnostics": self._last_cadence_diagnostics,
                "asset_class_last_run_at": {
                    asset_class: value.isoformat() + "Z"
                    for asset_class, value in self._last_asset_class_run_at.items()
                },
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

    def _loop_wake_interval_seconds(self) -> float:
        intervals = [
            self.settings.scan_interval_for_asset_class(asset_class)
            for asset_class in self._enabled_asset_classes()
        ]
        if not intervals:
            return max(1.0, float(self.settings.scan_interval_seconds))
        return max(1.0, float(min([self.settings.scan_interval_seconds, *intervals])))

    def _enabled_asset_classes(self) -> list[AssetClass]:
        ordered = [AssetClass.EQUITY, AssetClass.ETF, AssetClass.CRYPTO, AssetClass.OPTION]
        return [asset_class for asset_class in ordered if asset_class in self.settings.enabled_asset_class_set]

    def _configured_symbols_for_asset_class(self, asset_class: AssetClass) -> list[str]:
        candidate_groups: list[list[str]] = []
        if asset_class == AssetClass.CRYPTO:
            candidate_groups.append(list(self.settings.active_crypto_symbols))
            candidate_groups.append(list(self.settings.crypto_symbols))
        candidate_groups.append(list(self.settings.active_symbols))
        symbols: list[str] = []
        seen: set[str] = set()
        for group in candidate_groups:
            for symbol in group:
                try:
                    resolved_asset = self._resolve_asset(symbol, asset_class)
                except Exception:
                    continue
                if resolved_asset.asset_class != asset_class or resolved_asset.symbol in seen:
                    continue
                seen.add(resolved_asset.symbol)
                symbols.append(resolved_asset.symbol)
        return symbols

    def _open_symbols_by_asset_class(self) -> dict[AssetClass, list[str]]:
        grouped: dict[AssetClass, list[str]] = {}
        for position in self.portfolio.positions.values():
            if position.asset_class not in self.settings.enabled_asset_class_set:
                continue
            grouped.setdefault(position.asset_class, []).append(position.symbol)
        return grouped

    def _build_cadence_diagnostics(
        self,
        *,
        now: datetime.datetime,
        due_asset_classes: list[AssetClass],
        open_symbols_by_asset_class: dict[AssetClass, list[str]],
    ) -> dict[str, Any]:
        due_set = set(due_asset_classes)
        diagnostics: dict[str, Any] = {
            "checked_at": now.isoformat() + "Z",
            "due_asset_classes": [asset_class.value for asset_class in due_asset_classes],
            "skipped_asset_classes": {},
            "skipped_symbol_count": 0,
        }
        for asset_class in self._enabled_asset_classes():
            if asset_class in due_set:
                continue
            interval = self.settings.scan_interval_for_asset_class(asset_class)
            last_run_at = self._last_asset_class_run_at.get(asset_class.value)
            elapsed_seconds = None
            next_due_in_seconds = 0
            if last_run_at is not None:
                elapsed_seconds = max(0.0, (now - last_run_at).total_seconds())
                next_due_in_seconds = max(0.0, float(interval) - elapsed_seconds)
            configured_symbols = self._configured_symbols_for_asset_class(asset_class)
            open_symbols = list(dict.fromkeys(open_symbols_by_asset_class.get(asset_class, [])))
            skipped_symbols = list(dict.fromkeys(configured_symbols + open_symbols))
            diagnostics["skipped_asset_classes"][asset_class.value] = {
                "interval_seconds": interval,
                "elapsed_seconds": elapsed_seconds,
                "next_due_in_seconds": next_due_in_seconds,
                "configured_symbol_count": len(configured_symbols),
                "open_position_symbol_count": len(open_symbols),
                "skipped_symbol_count": len(skipped_symbols),
            }
            diagnostics["skipped_symbol_count"] += len(skipped_symbols)
        return diagnostics

    def _due_asset_classes(
        self,
        *,
        now: datetime.datetime,
        respect_cadence: bool,
    ) -> list[AssetClass]:
        if not respect_cadence:
            return self._enabled_asset_classes()
        due: list[AssetClass] = []
        for asset_class in self._enabled_asset_classes():
            interval = self.settings.scan_interval_for_asset_class(asset_class)
            last_run_at = self._last_asset_class_run_at.get(asset_class.value)
            if last_run_at is None or (now - last_run_at).total_seconds() >= interval:
                due.append(asset_class)
        return due

    def _mark_asset_classes_ran(
        self,
        asset_classes: list[AssetClass],
        *,
        ran_at: datetime.datetime,
    ) -> None:
        with self._state_lock:
            for asset_class in asset_classes:
                self._last_asset_class_run_at[asset_class.value] = ran_at

    def _run_loop(self) -> None:
        with self._state_lock:
            self._loop_thread_ident = threading.get_ident()
        while True:
            with self._state_lock:
                if not self._running:
                    break

            try:
                self._scan_and_trade(mode="background_loop", respect_cadence=True)
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

            if self._wake_event.wait(self._loop_wake_interval_seconds()):
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

        resolved_dust_positions: list[dict[str, Any]] = []
        try:
            resolved_dust_positions = self.execution_service.resolve_tracked_dust_positions(source="broker_sync")
        except Exception as exc:
            logger.warning("Failed to resolve tracked dust positions after broker sync: %s", exc)
        else:
            if resolved_dust_positions:
                with self._state_lock:
                    self._latest_dust_resolution = {
                        "source": "broker_sync",
                        "count": len(resolved_dust_positions),
                        "positions": resolved_dust_positions[-5:],
                        "resolved_at": datetime.datetime.utcnow().isoformat() + "Z",
                    }

        try:
            self.asset_catalog.ensure_fresh()
        except Exception as exc:
            logger.warning("Failed to refresh asset catalog: %s", exc)

        runtime_asset_class = self.settings.primary_runtime_asset_class
        try:
            self._market_open = self.broker.is_market_open(runtime_asset_class)
        except Exception:
            self._market_open = True
        try:
            session = self.market_data_service.get_session_status(runtime_asset_class)
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
        entry_bars: Any,
        *,
        strategy: Any,
        snapshot: NormalizedMarketSnapshot,
        entry_timeframe: str,
        regime_timeframe: str,
        regime_bars: Any = None,
        benchmark_bars: Any = None,
    ) -> StrategyContext:
        tracked_position = self.portfolio.get_position(asset.symbol)
        position_state = self.portfolio.get_position_state(asset.symbol)
        return StrategyContext(
            asset=asset,
            session=self.market_data_service.get_session_status(asset.asset_class),
            quote=self.market_data_service.get_latest_quote(asset.symbol, asset.asset_class),
            timeframe=entry_timeframe,
            metadata={
                "entry_bars": entry_bars,
                "regime_bars": regime_bars,
                "benchmark_bars": benchmark_bars,
                "entry_timeframe": entry_timeframe,
                "regime_timeframe": regime_timeframe,
                "short_selling_enabled": self.settings.short_selling_enabled,
                **position_state,
                "normalized_snapshot": snapshot.to_dict(),
                "tracked_position": (
                    {
                        "symbol": tracked_position.symbol,
                        "quantity": tracked_position.quantity,
                        "side": tracked_position.side,
                        "position_direction": tracked_position.direction.value,
                        "entry_price": tracked_position.entry_price,
                        "current_price": tracked_position.current_price,
                        "asset_class": tracked_position.asset_class.value,
                        "exchange": tracked_position.exchange,
                        "initial_quantity": tracked_position.initial_quantity,
                        "highest_price_since_entry": tracked_position.highest_price_since_entry,
                        "current_stop": tracked_position.current_stop,
                        "tp1_hit": tracked_position.tp1_hit,
                        "tp2_hit": tracked_position.tp2_hit,
                        "entry_signal_metadata": dict(tracked_position.entry_signal_metadata),
                    }
                    if tracked_position is not None
                    else None
                ),
            },
        )

    def _fetch_strategy_bars(
        self,
        asset: AssetMetadata,
        *,
        strategy: Any,
    ) -> tuple[Any, Any, Any, str, str]:
        entry_timeframe = self.settings.entry_timeframe_for_asset_class(asset.asset_class)
        regime_timeframe = self.settings.regime_timeframe_for_asset_class(asset.asset_class)
        lookback_bars = self.settings.lookback_bars_for_asset_class(asset.asset_class)
        strategy_name = getattr(strategy, "name", "")
        regime_long_sma = max(
            0,
            int(
                getattr(strategy, "regime_long_sma", 0)
                or getattr(strategy, "slow_window", 0)
                or 0
            ),
        )
        entry_limit = max(lookback_bars, 30)
        regime_limit = max(lookback_bars, regime_long_sma + 5 if regime_long_sma else 30)
        entry_bars = self.market_data_service.fetch_bars(
            asset.symbol,
            asset_class=asset.asset_class,
            timeframe=entry_timeframe,
            limit=entry_limit,
        )
        if regime_timeframe == entry_timeframe:
            regime_bars = entry_bars
        else:
            regime_bars = self.market_data_service.fetch_bars(
                asset.symbol,
                asset_class=asset.asset_class,
                timeframe=regime_timeframe,
                limit=regime_limit,
            )

        benchmark_bars = None
        regime_symbol = getattr(strategy, "regime_symbol", "SPY")
        if strategy_name == "equity_momentum_breakout":
            benchmark_limit = max(regime_limit, regime_long_sma + 5 if regime_long_sma else 30)
            benchmark_symbol = regime_symbol if asset.symbol != regime_symbol else asset.symbol
            benchmark_asset_class = AssetClass.ETF if benchmark_symbol == regime_symbol else asset.asset_class
            benchmark_bars = self.market_data_service.fetch_bars(
                benchmark_symbol,
                asset_class=benchmark_asset_class,
                timeframe=regime_timeframe,
                limit=benchmark_limit,
            )
        return entry_bars, regime_bars, benchmark_bars, entry_timeframe, regime_timeframe

    def _get_normalized_snapshot(
        self,
        asset: AssetMetadata,
        precomputed_snapshot: dict[str, Any] | None = None,
    ) -> NormalizedMarketSnapshot:
        if precomputed_snapshot:
            return NormalizedMarketSnapshot.from_dict(precomputed_snapshot)
        return self.market_data_service.get_normalized_snapshot(asset.symbol, asset.asset_class)

    def _apply_snapshot_metadata(
        self,
        signal: TradeSignal,
        *,
        asset: AssetMetadata,
        normalized_snapshot: NormalizedMarketSnapshot,
        evaluation_mode: str,
    ) -> TradeSignal:
        if normalized_snapshot.evaluation_price is not None:
            evaluation_price = float(normalized_snapshot.evaluation_price)
            if signal.signal_type == "entry" and signal.signal in {Signal.BUY, Signal.SELL}:
                self._rebase_signal_levels(signal, evaluation_price)
            signal.price = evaluation_price
            signal.entry_price = evaluation_price

        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics["evaluation_mode"] = evaluation_mode
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
        signal.metrics["exchange"] = normalized_snapshot.exchange or asset.exchange
        signal.metrics["source"] = normalized_snapshot.source
        return signal

    def _evaluate_exit_signal(
        self,
        asset: AssetMetadata,
        *,
        normalized_snapshot: NormalizedMarketSnapshot,
        evaluation_mode: str,
        regime_snapshot: dict[str, Any] | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> TradeSignal | None:
        exit_model_result = None
        position = self.portfolio.get_position(asset.symbol)
        if position is not None and self.settings.ml_enabled and self.settings.exit_model_enabled:
            exit_probe = self._build_exit_model_probe(
                asset=asset,
                position=position,
                evaluation_price=normalized_snapshot.evaluation_price,
            )
            exit_model_result = self.ml_scorer.score_exit_signal(
                exit_probe,
                market_overview=regime_snapshot,
                news_features=news_features,
                latest_price=normalized_snapshot.evaluation_price,
            )
        evaluation = self.exit_manager.evaluate_long_position(
            asset.symbol,
            normalized_snapshot.evaluation_price,
            asset_class=asset.asset_class,
            current_bar_index=self.tranche_state.get_scan_bar_index(),
            regime_state=self._resolve_market_regime_label(regime_snapshot),
            news_features=news_features,
            exit_model_score=exit_model_result.score if exit_model_result is not None else None,
        )
        signal = evaluation.signal
        if signal is None:
            return None
        signal = self._apply_snapshot_metadata(
            signal,
            asset=asset,
            normalized_snapshot=normalized_snapshot,
            evaluation_mode=evaluation_mode,
        )
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics["strategy_selected"] = signal.strategy_name
        signal.metrics["decision_code"] = signal.metrics.get("decision_code") or "exit_signal"
        signal.metrics["spread_pct"] = normalized_snapshot.spread_pct
        signal.metrics["exit_state"] = evaluation.state
        if exit_model_result is not None:
            signal.metrics["exit_ml"] = exit_model_result.to_dict()
        return signal

    def _build_exit_model_probe(
        self,
        *,
        asset: AssetMetadata,
        position: Any,
        evaluation_price: float | None,
    ) -> TradeSignal:
        metadata = dict(position.entry_signal_metadata)
        return TradeSignal(
            symbol=asset.symbol,
            signal=Signal.SELL,
            asset_class=asset.asset_class,
            strategy_name=str(metadata.get("strategy_name") or "exit_model"),
            signal_type="exit",
            order_intent="long_exit",
            reduce_only=True,
            exit_stage="ml_exit",
            price=evaluation_price,
            entry_price=evaluation_price,
            atr=self._safe_float(metadata.get("atr")),
            stop_price=self._safe_float(position.current_stop or metadata.get("stop_price")),
            target_price=self._safe_float(metadata.get("target_price")),
            metrics={
                "current_stop": position.current_stop,
                "holding_duration_bars": (
                    self.tranche_state.get_scan_bar_index() - int(metadata.get("entry_scan_bar_index"))
                    if metadata.get("entry_scan_bar_index") not in {None, ""}
                    else None
                ),
                "unrealized_return": (
                    ((evaluation_price or position.current_price) - position.entry_price) / position.entry_price
                    if position.entry_price
                    else None
                ),
                "favorable_excursion_r": (
                    ((position.highest_price_since_entry or position.entry_price) - position.entry_price)
                    / max(position.entry_price - float(metadata.get("stop_price") or position.current_stop or position.entry_price), 1e-9)
                    if metadata.get("stop_price") is not None or position.current_stop is not None
                    else None
                ),
                "hit_target_stages": list(metadata.get("hit_target_stages") or []),
            },
        )

    @staticmethod
    def _resolve_market_regime_label(regime_snapshot: dict[str, Any] | None) -> str | None:
        if not regime_snapshot:
            return None
        bullish = float(regime_snapshot.get("bullish") or 0.0)
        bearish = float(regime_snapshot.get("bearish") or 0.0)
        if bearish > bullish:
            return "bearish"
        if bullish > bearish:
            return "bullish"
        return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _evaluate_asset(
        self,
        asset: AssetMetadata,
        prefer_primary_strategy: bool = False,
        *,
        evaluation_mode: str = "auto",
        precomputed_snapshot: dict[str, Any] | None = None,
    ) -> TradeSignal:
        strategy = self._select_strategy_for_asset(asset)
        normalized_snapshot = self._get_normalized_snapshot(asset, precomputed_snapshot)

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

        entry_bars, regime_bars, benchmark_bars, entry_timeframe, regime_timeframe = self._fetch_strategy_bars(
            asset,
            strategy=strategy,
        )
        context = self._build_context(
            asset,
            entry_bars,
            strategy=strategy,
            snapshot=normalized_snapshot,
            entry_timeframe=entry_timeframe,
            regime_timeframe=regime_timeframe,
            regime_bars=regime_bars,
            benchmark_bars=benchmark_bars,
        )
        candidate_signals: list[TradeSignal] = []
        strategy_input: Any = entry_bars
        strategy_name = getattr(strategy, "name", "")
        if strategy_name == "equity_momentum_breakout":
            strategy_input = {
                "symbol": entry_bars,
                "benchmark": benchmark_bars,
                "regime": regime_bars,
            }
        elif strategy_name == "crypto_momentum_trend" and regime_timeframe != entry_timeframe:
            strategy_input = {
                "entry": entry_bars,
                "regime": regime_bars,
            }
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
        signal = self._normalize_signal_for_position_context(asset, signal)
        signal = self._apply_snapshot_metadata(
            signal,
            asset=asset,
            normalized_snapshot=normalized_snapshot,
            evaluation_mode=evaluation_mode,
        )
        signal.liquidity_score = signal.liquidity_score or 0.0
        if signal.metrics is None:
            signal.metrics = {}
        latest_volume = float(entry_bars.iloc[-1]["Volume"]) if not entry_bars.empty else None
        signal.metrics.setdefault("avg_volume", float(entry_bars["Volume"].tail(10).mean()) if not entry_bars.empty else None)
        signal.metrics.setdefault("dollar_volume", (signal.metrics.get("avg_volume") or 0.0) * (signal.entry_price or signal.price or 0.0))
        signal.metrics.setdefault("latest_volume", latest_volume)
        signal.metrics["spread_pct"] = normalized_snapshot.spread_pct
        signal.metrics["decision_code"] = signal.metrics.get("decision_code") or ("no_signal" if signal.signal == Signal.HOLD else "signal")
        signal.metrics["strategy_selected"] = strategy.name
        signal.metrics["entry_timeframe"] = entry_timeframe
        signal.metrics["regime_timeframe"] = regime_timeframe
        signal.metrics["entry_bar_count"] = len(entry_bars)
        signal.metrics["regime_bar_count"] = len(regime_bars) if regime_bars is not None else len(entry_bars)
        return signal

    @staticmethod
    def _rebase_signal_levels(signal: TradeSignal, new_entry_price: float) -> None:
        if signal.signal not in {Signal.BUY, Signal.SELL}:
            return

        original_entry_price = signal.entry_price if signal.entry_price is not None else signal.price
        if original_entry_price is None:
            return

        original_entry_price = float(original_entry_price)
        if abs(original_entry_price - new_entry_price) < 1e-9:
            return

        if signal.stop_price is not None:
            signal.stop_price = float(new_entry_price + (float(signal.stop_price) - original_entry_price))
        if signal.target_price is not None:
            signal.target_price = float(new_entry_price + (float(signal.target_price) - original_entry_price))

    def _normalize_signal_for_position_context(self, asset: AssetMetadata, signal: TradeSignal) -> TradeSignal:
        has_tracked_position = self.portfolio.get_position(asset.symbol) is not None
        has_sellable_long_position = self.portfolio.is_sellable_long_position(asset.symbol)
        has_coverable_short_position = self.portfolio.is_coverable_short_position(asset.symbol)
        tracked_position = self.portfolio.get_position(asset.symbol)
        if signal.metrics is None:
            signal.metrics = {}
        signal.metrics.setdefault("has_tracked_position", has_tracked_position)
        signal.metrics.setdefault("has_tracked_long_position", has_sellable_long_position)
        signal.metrics.setdefault("has_sellable_long_position", has_sellable_long_position)
        signal.metrics.setdefault("has_coverable_short_position", has_coverable_short_position)
        signal.metrics.setdefault("short_selling_enabled", self.settings.short_selling_enabled)
        signal.metrics.setdefault(
            "tracked_position_direction",
            tracked_position.direction.value if tracked_position is not None else None,
        )

        if signal.signal == Signal.SELL:
            if has_sellable_long_position:
                signal.signal_type = "exit"
                signal.order_intent = "long_exit"
                signal.reduce_only = True
            elif signal.order_intent == "long_exit":
                return self._build_blocked_hold_signal(
                    signal,
                    decision_code="no_position_to_sell",
                    blocked_rule="no_position_to_sell",
                    blocked_reason="Exit-only sell ignored: no tracked long position is available to exit.",
                )
            elif signal.order_intent == "short_entry":
                if not self.settings.short_selling_enabled:
                    return self._build_blocked_hold_signal(
                        signal,
                        decision_code="short_selling_disabled",
                        blocked_rule="short_selling_disabled",
                        blocked_reason="Short entry ignored because short selling is disabled.",
                    )
                signal.signal_type = "entry"
                signal.reduce_only = False
            elif self.settings.short_selling_enabled:
                signal.signal_type = "entry"
                signal.order_intent = "short_entry"
                signal.reduce_only = False
            else:
                return self._build_blocked_hold_signal(
                    signal,
                    decision_code="no_position_to_sell",
                    blocked_rule="no_position_to_sell",
                    blocked_reason="Exit-only sell ignored: no tracked long position and short selling is disabled.",
                )
        elif signal.signal == Signal.BUY:
            if has_coverable_short_position:
                signal.signal_type = "exit"
                signal.order_intent = "short_exit"
                signal.reduce_only = True
            elif signal.order_intent == "short_exit":
                return self._build_blocked_hold_signal(
                    signal,
                    decision_code="no_position_to_cover",
                    blocked_rule="no_position_to_cover",
                    blocked_reason="Cover-only buy ignored: no tracked short position is available to cover.",
                )
            else:
                signal.signal_type = "entry"
                signal.order_intent = "long_entry"
                signal.reduce_only = False

        signal.apply_intent_defaults()
        signal.metrics["position_direction"] = (
            signal.direction.value
            if signal.direction != SignalDirection.FLAT
            else (tracked_position.direction.value if tracked_position is not None else None)
        )
        signal.metrics["is_risk_reducing_order"] = signal.order_intent in {"long_exit", "short_exit"} or signal.reduce_only
        signal.metrics["is_risk_reducing_sell"] = (
            signal.signal == Signal.SELL and signal.metrics["is_risk_reducing_order"]
        )
        return signal

    def _build_blocked_hold_signal(
        self,
        signal: TradeSignal,
        *,
        decision_code: str,
        blocked_rule: str,
        blocked_reason: str,
    ) -> TradeSignal:
        hold_signal = TradeSignal(
            symbol=signal.symbol,
            signal=Signal.HOLD,
            asset_class=signal.asset_class,
            strategy_name=signal.strategy_name,
            signal_type=signal.signal_type,
            order_intent=signal.order_intent,
            reduce_only=signal.reduce_only,
            exit_fraction=signal.exit_fraction,
            exit_stage=signal.exit_stage,
            confidence_score=0.0,
            price=signal.price,
            entry_price=signal.entry_price,
            reason=f"{signal.reason} {blocked_reason}".strip() if signal.reason else blocked_reason,
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
                "original_signal": signal.signal.value,
                "decision_code": decision_code,
                "blocked_rule": blocked_rule,
                "blocked_reason": blocked_reason,
            },
        )
        hold_signal.apply_intent_defaults()
        return hold_signal

    def _merge_scan_results(
        self,
        scan_results_by_asset_class: dict[str, ScanResult],
    ) -> ScanResult:
        ordered_results = [
            scan_results_by_asset_class[key]
            for key in sorted(scan_results_by_asset_class.keys())
        ]
        opportunities = [
            opportunity
            for result in ordered_results
            for opportunity in result.opportunities
        ]
        sorted_by_quality = sorted(
            opportunities,
            key=lambda item: item.signal_quality_score,
            reverse=True,
        )
        regime_status: dict[str, int] = {}
        symbol_snapshots: dict[str, Any] = {}
        prefilter_counts: dict[str, int] = {}
        final_evaluation_counts: dict[str, int] = {}
        timeframes_by_asset_class: dict[str, dict[str, Any]] = {}
        symbol_inclusion_reasons: dict[str, list[str]] = {}
        selection_diagnostics: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        scanned_count = 0
        for result in ordered_results:
            scanned_count += result.scanned_count
            errors.extend(result.errors)
            symbol_snapshots.update(result.symbol_snapshots)
            prefilter_counts.update(result.prefilter_counts)
            final_evaluation_counts.update(result.final_evaluation_counts)
            timeframes_by_asset_class.update(result.timeframes_by_asset_class)
            symbol_inclusion_reasons.update(result.symbol_inclusion_reasons)
            selection_diagnostics.update(result.selection_diagnostics)
            for label, count in result.regime_status.items():
                regime_status[label] = regime_status.get(label, 0) + count

        limit = max(len(sorted_by_quality), 1)
        return ScanResult(
            generated_at=max((result.generated_at for result in ordered_results), default=datetime.datetime.utcnow()),
            asset_class=ordered_results[0].asset_class if len(ordered_results) == 1 else None,
            scanned_count=scanned_count,
            opportunities=sorted_by_quality[:limit],
            top_gainers=sorted(
                opportunities,
                key=lambda item: item.price_change_pct if item.price_change_pct is not None else -999.0,
                reverse=True,
            )[:limit],
            top_losers=sorted(
                opportunities,
                key=lambda item: item.price_change_pct if item.price_change_pct is not None else 999.0,
            )[:limit],
            unusual_volume=sorted(
                opportunities,
                key=lambda item: item.metrics.get("relative_volume", 0.0),
                reverse=True,
            )[:limit],
            breakouts=[item for item in sorted_by_quality if "breakout" in item.tags][:limit],
            pullbacks=[item for item in sorted_by_quality if "pullback" in item.tags][:limit],
            volatility=sorted(opportunities, key=lambda item: item.volatility_score, reverse=True)[:limit],
            momentum=sorted(opportunities, key=lambda item: item.momentum_score, reverse=True)[:limit],
            regime_status=regime_status,
            errors=errors,
            symbol_snapshots=symbol_snapshots,
            prefilter_counts=prefilter_counts,
            final_evaluation_counts=final_evaluation_counts,
            timeframes_by_asset_class=timeframes_by_asset_class,
            symbol_inclusion_reasons=symbol_inclusion_reasons,
            selection_diagnostics=selection_diagnostics,
        )

    def _scan_and_trade(self, *, mode: str = "auto", respect_cadence: bool = False) -> List[Dict[str, Any]]:
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
                    "active_asset_classes": self.settings.active_asset_classes,
                    "crypto_only_mode": self.settings.crypto_only_mode,
                    "strategy_name": self.settings.active_strategy,
                    "dry_run": not self.settings.trading_enabled,
                    "universe_scan_enabled": self.settings.universe_scan_enabled,
                },
            )

            with self._execution_lock:
                self._sync_portfolio_from_broker()
                open_symbols_by_asset_class = self._open_symbols_by_asset_class()
                due_asset_classes = self._due_asset_classes(now=now, respect_cadence=respect_cadence)
                cadence_diagnostics = self._build_cadence_diagnostics(
                    now=now,
                    due_asset_classes=due_asset_classes,
                    open_symbols_by_asset_class=open_symbols_by_asset_class,
                )
                if respect_cadence and not due_asset_classes:
                    with self._state_lock:
                        self._last_cadence_diagnostics = cadence_diagnostics
                        self._last_run_result = {
                            "mode": mode,
                            "status": "skipped",
                            "reason": "cadence_not_due",
                            "cadence": cadence_diagnostics,
                            "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                        }
                    return []

                scan_bar_index = self.tranche_state.increment_scan_bar_index()
                scan_results_by_asset_class: dict[str, ScanResult] = {}
                scan_selection_mode_by_asset_class: dict[str, str] = {}
                requested_scan_symbols_by_asset_class: dict[str, list[str]] = {}
                scan_ranking_limit_by_asset_class: dict[str, int] = {}

                for asset_class in due_asset_classes:
                    asset_key = asset_class.value
                    requested_scan_symbols = self._configured_symbols_for_asset_class(asset_class)
                    open_symbols = list(dict.fromkeys(open_symbols_by_asset_class.get(asset_class, [])))
                    inclusion_reasons = {
                        symbol: ["open_position"]
                        for symbol in open_symbols
                    }
                    if self.settings.crypto_only_mode or not self.settings.universe_scan_enabled:
                        scan_selection_mode = "configured_active_symbols"
                        requested_scan_symbols = list(dict.fromkeys(requested_scan_symbols + open_symbols))
                        scan_ranking_limit = max(1, len(requested_scan_symbols))
                        for symbol in requested_scan_symbols:
                            reasons = inclusion_reasons.setdefault(symbol, [])
                            if symbol in open_symbols:
                                reasons.append("open_position")
                            if symbol in self._configured_symbols_for_asset_class(asset_class):
                                reasons.append("configured_active_symbol")
                        scan_result = self.scanner.scan(
                            asset_class=asset_class,
                            symbols=requested_scan_symbols,
                            limit=scan_ranking_limit,
                            inclusion_reasons=inclusion_reasons,
                        )
                    else:
                        scan_selection_mode = "ranked_prefilter_universe"
                        scan_ranking_limit = max(5, self.settings.final_evaluation_limit_for_asset_class(asset_class))
                        scan_result = self.scanner.scan(
                            asset_class=asset_class,
                            limit=scan_ranking_limit,
                            required_symbols=open_symbols,
                            inclusion_reasons=inclusion_reasons,
                        )
                    scan_results_by_asset_class[asset_key] = scan_result
                    scan_selection_mode_by_asset_class[asset_key] = scan_selection_mode
                    requested_scan_symbols_by_asset_class[asset_key] = requested_scan_symbols
                    scan_ranking_limit_by_asset_class[asset_key] = scan_ranking_limit

                scan_result = self._merge_scan_results(scan_results_by_asset_class)
                all_symbols = list((scan_result.symbol_snapshots or {}).keys())
                assets = [self._resolve_asset(symbol) for symbol in all_symbols]
                snapshot_by_symbol = scan_result.symbol_snapshots or {}
                opportunity_by_symbol = {item.symbol: item for item in scan_result.opportunities}
                inclusion_reasons_by_symbol = scan_result.symbol_inclusion_reasons or {}
                timeframes_by_asset_class = scan_result.timeframes_by_asset_class or {}
                news_features_by_symbol = self._load_news_features(all_symbols)

                signals: list[TradeSignal] = []
                symbol_evaluations: list[dict[str, Any]] = []
                latest_signals: dict[str, Any] = {}
                for asset in assets:
                    normalized_snapshot = self._get_normalized_snapshot(
                        asset,
                        snapshot_by_symbol.get(asset.symbol),
                    )
                    signal = self._evaluate_exit_signal(
                        asset,
                        normalized_snapshot=normalized_snapshot,
                        evaluation_mode="auto",
                        regime_snapshot=scan_result.regime_status,
                        news_features=news_features_by_symbol.get(asset.symbol),
                    )
                    if signal is None:
                        signal = self._evaluate_asset(
                            asset,
                            evaluation_mode="auto",
                            precomputed_snapshot=normalized_snapshot.to_dict(),
                        )
                    self._enrich_signal(
                        signal,
                        cycle_id=cycle_id,
                        scan_bar_index=scan_bar_index,
                        regime_snapshot=scan_result.regime_status,
                        ranked_opportunity=opportunity_by_symbol.get(asset.symbol),
                        news_features=news_features_by_symbol.get(asset.symbol),
                    )
                    signal = self._apply_ml_score_filter(
                        signal,
                        regime_snapshot=scan_result.regime_status,
                        news_features=news_features_by_symbol.get(asset.symbol),
                    )
                    latest_signals[asset.symbol] = signal.to_dict()
                    decision_code = str((signal.metrics or {}).get("decision_code") or "")
                    evaluation_action = self._classify_evaluation_action(signal)
                    signal_snapshot = (signal.metrics or {}).get("normalized_snapshot", {})
                    price_source_used = (signal.metrics or {}).get("price_source_used") or signal_snapshot.get("price_source_used")
                    class_timeframes = timeframes_by_asset_class.get(asset.asset_class.value, {})
                    ranked_opportunity = opportunity_by_symbol.get(asset.symbol)
                    symbol_evaluations.append(
                        {
                            "symbol": asset.symbol,
                            "asset_class": asset.asset_class.value,
                            "strategy_selected": (signal.metrics or {}).get("strategy_selected", signal.strategy_name),
                            "initial_action": evaluation_action,
                            "market_session_state": signal_snapshot.get("session_state"),
                            "latest_normalized_snapshot": signal_snapshot,
                            "quote_available": (signal.metrics or {}).get("quote_available"),
                            "exchange": (signal.metrics or {}).get("exchange") or signal_snapshot.get("exchange"),
                            "data_source": (signal.metrics or {}).get("source") or signal_snapshot.get("source"),
                            "price_source_for_ranking": (
                                (scan_result.symbol_snapshots.get(asset.symbol, {}) if scan_result.symbol_snapshots else {}).get("price_source_used")
                                or price_source_used
                            ),
                            "price_source_for_signal": price_source_used,
                            "price_source_for_order_proposal": price_source_used,
                            "price_source_for_spread_check": price_source_used,
                            "latest_price": signal.price,
                            "signal": signal.signal.value,
                            "action": evaluation_action,
                            "classification": normalize_outcome_classification(evaluation_action, decision_code),
                            "decision_rule": decision_code or None,
                            "decision_reason": signal.reason,
                            "scanner_timeframe": class_timeframes.get("scanner_timeframe"),
                            "entry_timeframe": (signal.metrics or {}).get("entry_timeframe") or class_timeframes.get("entry_timeframe"),
                            "regime_timeframe": (signal.metrics or {}).get("regime_timeframe") or class_timeframes.get("regime_timeframe"),
                            "scan_inclusion_reasons": inclusion_reasons_by_symbol.get(asset.symbol, []),
                            "prefilter_score": (
                                (ranked_opportunity.metrics or {}).get("prefilter_score")
                                if ranked_opportunity is not None
                                else None
                            ),
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
                ranked_buy_signals = self._rank_buy_signals(buy_signals)
                selected_buy_signals = self._select_ranked_buy_signals(ranked_buy_signals)
                buy_ranking_by_symbol = {
                    signal.symbol: dict((signal.metrics or {}).get("buy_ranking") or {})
                    for signal in ranked_buy_signals
                }
                for row in symbol_evaluations:
                    ranking = buy_ranking_by_symbol.get(row["symbol"])
                    if ranking:
                        row["ranking"] = ranking
                        row["combined_score"] = ranking.get("combined_score")
                        row["strategy_score"] = ranking.get("strategy_score")
                        row["entry_ml_score"] = ranking.get("entry_ml_score")
                        row["risk_quality_adjustment"] = ranking.get("risk_quality_adjustment")
                selected_signals = skipped_hold_signals + sell_signals
                if not sell_signals:
                    selected_signals += selected_buy_signals

                for signal in selected_signals:
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
                                or row.get("price_source_for_order_proposal")
                            )
                            row["price_source_for_spread_check"] = (
                                (execution.get("risk") or {}).get("details", {}).get("price_source_used")
                                or row.get("price_source_for_spread_check")
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
                        ranking = buy_ranking_by_symbol.get(row["symbol"]) or {}
                        row["action"] = "skipped"
                        row["decision_rule"] = ranking.get("selection_rule") or "not_selected_by_rank"
                        row["decision_reason"] = ranking.get("selection_reason") or "Candidate not selected this cycle."

                outcome_counts = self._count_outcomes(symbol_evaluations)
                signal_funnel = self._count_signal_funnel(symbol_evaluations, outcome_counts)
                self._observe_broker_order_statuses(cycle_id=cycle_id)
                self._mark_asset_classes_ran(due_asset_classes, ran_at=now)

                with self._state_lock:
                    self._last_run_time = now
                    self._last_cycle_id = cycle_id
                    self._last_scanned_symbols = all_symbols
                    self._last_signals = latest_signals
                    self._last_ranked_candidates = [
                        {
                            "symbol": signal.symbol,
                            "asset_class": signal.asset_class.value,
                            "signal": signal.signal.value,
                            "combined_score": ((signal.metrics or {}).get("buy_ranking") or {}).get("combined_score"),
                            "strategy_score": ((signal.metrics or {}).get("buy_ranking") or {}).get("strategy_score"),
                            "entry_ml_score": ((signal.metrics or {}).get("buy_ranking") or {}).get("entry_ml_score"),
                            "risk_quality_adjustment": ((signal.metrics or {}).get("buy_ranking") or {}).get("risk_quality_adjustment"),
                            "selection_rule": ((signal.metrics or {}).get("buy_ranking") or {}).get("selection_rule"),
                            "selection_reason": ((signal.metrics or {}).get("buy_ranking") or {}).get("selection_reason"),
                        }
                        for signal in ranked_buy_signals
                    ] or [item.to_dict() for item in scan_result.opportunities]
                    self._last_regime_snapshot = scan_result.regime_status
                    self._last_scan_overview = scan_result.to_dict()
                    self._last_cadence_diagnostics = cadence_diagnostics
                    self._last_scan_overview["scan_bar_index"] = scan_bar_index
                    self._last_scan_overview["cycle_id"] = cycle_id
                    self._last_scan_overview["mode"] = mode
                    self._last_scan_overview["outcome_counts"] = outcome_counts
                    self._last_scan_overview["signal_funnel"] = signal_funnel
                    self._last_scan_overview["scan_selection_mode"] = "per_asset_class_cadence"
                    self._last_scan_overview["scan_selection_mode_by_asset_class"] = scan_selection_mode_by_asset_class
                    self._last_scan_overview["scan_requested_symbols"] = list(
                        dict.fromkeys(
                            symbol
                            for symbols in requested_scan_symbols_by_asset_class.values()
                            for symbol in symbols
                        )
                    )
                    self._last_scan_overview["scan_requested_symbols_by_asset_class"] = requested_scan_symbols_by_asset_class
                    self._last_scan_overview["scan_ranking_limit"] = max(scan_ranking_limit_by_asset_class.values(), default=0)
                    self._last_scan_overview["scan_ranking_limit_by_asset_class"] = scan_ranking_limit_by_asset_class
                    self._last_scan_overview["due_asset_classes"] = [asset_class.value for asset_class in due_asset_classes]
                    self._last_scan_overview["cadence"] = cadence_diagnostics
                    self._last_scan_overview["evaluated_symbol_count"] = len(symbol_evaluations)
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
            "initial_action": self._classify_evaluation_action(signal),
            "market_session_state": snapshot.get("session_state"),
            "latest_normalized_snapshot": snapshot,
            "quote_available": snapshot.get("quote_available"),
            "exchange": (signal.metrics or {}).get("exchange") or snapshot.get("exchange"),
            "data_source": (signal.metrics or {}).get("source") or snapshot.get("source"),
            "price_source_for_ranking": snapshot.get("price_source_used"),
            "price_source_for_signal": snapshot.get("price_source_used"),
            "price_source_for_order_proposal": (execution.get("risk") or {}).get("details", {}).get("price_source_used") or snapshot.get("price_source_used"),
            "price_source_for_spread_check": (execution.get("risk") or {}).get("details", {}).get("price_source_used") or snapshot.get("price_source_used"),
            "scanner_timeframe": (signal.metrics or {}).get("scanner_timeframe"),
            "entry_timeframe": (signal.metrics or {}).get("entry_timeframe"),
            "regime_timeframe": (signal.metrics or {}).get("regime_timeframe"),
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
        order_id = str(order.get("id") or order.get("client_order_id") or "")
        with self._state_lock:
            self._last_order = order
            self._last_submitted_order = order
            if order_id:
                self._session_order_ids.add(order_id)

    def _track_execution_result(self, result: Dict[str, Any]) -> None:
        with self._state_lock:
            action = str(result.get("action") or "")
            risk = result.get("risk") or {}
            if action == "rejected":
                self._last_rejected_candidate = result
                self._latest_rejected_reason = risk.get("reason")
            elif action == "dust_resolved":
                self._latest_dust_resolution = result
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
        counts: dict[str, int] = {"submitted": 0, "rejected": 0, "skipped": 0, "hold": 0, "dust_resolved": 0}

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

    def _count_signal_funnel(
        self,
        evaluations: list[dict[str, Any]],
        outcome_counts: dict[str, int],
    ) -> dict[str, int]:
        return {
            "hold": sum(1 for row in evaluations if str(row.get("signal") or "").upper() == Signal.HOLD.value),
            "candidate": sum(1 for row in evaluations if str(row.get("initial_action") or "").lower() == "candidate"),
            "submitted": int(outcome_counts.get("submitted", 0)),
            "rejected": int(outcome_counts.get("rejected", 0)),
            "skipped": int(outcome_counts.get("skipped", 0)),
            "dust_resolved": int(outcome_counts.get("dust_resolved", 0)),
        }

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

    def _broker_status_cache_path(self) -> Path:
        path = Path(self.settings.broker_order_status_cache_path)
        if not path.is_absolute():
            return path
        return path

    def _load_broker_order_status_memory(self) -> Dict[str, Dict[str, Any]]:
        cache_path = self._broker_status_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            return {}

        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load broker order status cache; using empty state",
                extra={"cache_path": str(cache_path), "error": str(exc)},
            )
            return {}

        memory: Dict[str, Dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return memory

        for order_id, raw_entry in payload.items():
            if not isinstance(order_id, str) or not isinstance(raw_entry, dict):
                continue
            status = self._normalize_broker_status(raw_entry.get("status"))
            if status is None:
                continue
            memory[order_id] = {
                "status": status,
                "last_seen_at": str(raw_entry.get("last_seen_at") or ""),
                "symbol": str(raw_entry.get("symbol") or "") or None,
                "side": str(raw_entry.get("side") or "") or None,
                "ignored_on_startup": bool(raw_entry.get("ignored_on_startup", False)),
            }
        return memory

    def _persist_broker_order_status_memory(self) -> None:
        cache_path = self._broker_status_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(
                json.dumps(self._order_status_memory, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Failed to persist broker order status cache",
                extra={"cache_path": str(cache_path), "error": str(exc)},
            )

    def _resolve_broker_order_timestamp(self, order: dict[str, Any]) -> datetime.datetime | None:
        for field_name in ("filled_at", "updated_at", "submitted_at", "executed_at", "created_at"):
            raw_value = order.get(field_name)
            if raw_value in {None, ""}:
                continue
            try:
                parsed = parse_iso_datetime(raw_value)
            except ValueError:
                continue
            if parsed is None:
                continue
            return parsed.astimezone(datetime.timezone.utc)
        return None

    def _build_broker_order_status_entry(
        self,
        *,
        order: dict[str, Any],
        status: str,
        observed_at: datetime.datetime,
        ignored_on_startup: bool = False,
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "last_seen_at": observed_at.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "symbol": str(order.get("symbol") or "") or None,
            "side": str(order.get("side") or "") or None,
            "ignored_on_startup": ignored_on_startup,
        }

    def _should_ignore_terminal_baseline_order(
        self,
        *,
        order: dict[str, Any],
        status: str,
        observed_at: datetime.datetime,
    ) -> bool:
        if status not in {"filled", "canceled", "rejected"}:
            return False
        threshold_minutes = self.settings.broker_order_status_ignore_terminal_older_than_minutes
        if threshold_minutes <= 0:
            return False
        order_timestamp = self._resolve_broker_order_timestamp(order)
        if order_timestamp is None:
            return False
        return order_timestamp <= (
            observed_at - datetime.timedelta(minutes=threshold_minutes)
        )

    def _seed_broker_order_status_memory_from_existing_orders(
        self,
        orders: list[dict[str, Any]],
        *,
        observed_at: datetime.datetime,
        exclude_order_ids: set[str] | None = None,
    ) -> None:
        excluded = exclude_order_ids or set()
        changed = False
        for order in orders:
            order_id = str(order.get("id") or order.get("client_order_id") or "")
            if not order_id or order_id in excluded:
                continue
            status = self._normalize_broker_status(order.get("status"))
            if status is None:
                continue
            self._order_status_memory[order_id] = self._build_broker_order_status_entry(
                order=order,
                status=status,
                observed_at=observed_at,
                ignored_on_startup=self._should_ignore_terminal_baseline_order(
                    order=order,
                    status=status,
                    observed_at=observed_at,
                ),
            )
            changed = True

        if changed:
            self._persist_broker_order_status_memory()

    def _observe_broker_order_statuses(self, *, cycle_id: str) -> None:
        try:
            orders = self.broker.list_orders()
        except Exception as exc:
            logger.warning("Failed to poll broker order statuses: %s", exc)
            return

        notifier = get_discord_notifier(self.settings)
        updates: list[dict[str, Any]] = []
        observed_at = datetime.datetime.now(datetime.timezone.utc)
        if not self._broker_status_baseline_synced:
            if self.settings.broker_order_status_suppress_startup_replay:
                self._seed_broker_order_status_memory_from_existing_orders(
                    orders,
                    observed_at=observed_at,
                    exclude_order_ids=set(self._session_order_ids),
                )
            self._broker_status_baseline_synced = True

        cache_dirty = False
        for order in orders:
            order_id = str(order.get("id") or order.get("client_order_id") or "")
            if not order_id:
                continue
            status = self._normalize_broker_status(order.get("status"))
            if status is None:
                continue

            with self._state_lock:
                previous = self._order_status_memory.get(order_id) or {}
                previous_status = previous.get("status")
                next_entry = self._build_broker_order_status_entry(
                    order=order,
                    status=status,
                    observed_at=observed_at,
                    ignored_on_startup=False,
                )
                if previous_status == status:
                    next_entry["ignored_on_startup"] = bool(previous.get("ignored_on_startup", False))
                    self._order_status_memory[order_id] = next_entry
                    cache_dirty = True
                    self._broker_status_dedupe_suppressed += 1
                    continue
                self._order_status_memory[order_id] = next_entry
                cache_dirty = True

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

        if cache_dirty:
            self._persist_broker_order_status_memory()
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
        scan_bar_index: int | None = None,
        regime_snapshot: dict[str, Any] | None = None,
        ranked_opportunity: Any | None = None,
        news_features: dict[str, Any] | None = None,
    ) -> None:
        if signal.metrics is None:
            signal.metrics = {}
        position_state = self.portfolio.get_position_state(signal.symbol)
        signal.metrics.setdefault("signal_id", build_signal_id(signal.symbol, signal.strategy_name, signal.generated_at))
        signal.metrics["cycle_id"] = cycle_id
        if scan_bar_index is not None:
            signal.metrics["scan_bar_index"] = scan_bar_index
        signal.metrics["market_overview"] = dict(regime_snapshot or {})
        signal.metrics.setdefault("has_tracked_position", position_state["has_tracked_position"])
        signal.metrics.setdefault("has_sellable_long_position", position_state["has_sellable_long_position"])
        signal.metrics.setdefault("has_coverable_short_position", position_state["has_coverable_short_position"])
        signal.metrics.setdefault("position_direction", position_state["position_direction"])
        signal.metrics.setdefault("tracked_position_direction", position_state["position_direction"])
        signal.metrics.setdefault("highest_price_since_entry", position_state["highest_price_since_entry"])
        signal.metrics.setdefault("current_stop", position_state["current_stop"])
        signal.metrics.setdefault("tp1_hit", position_state["tp1_hit"])
        signal.metrics.setdefault("tp2_hit", position_state["tp2_hit"])
        if ranked_opportunity is not None:
            signal.metrics["scan_signal_quality_score"] = getattr(ranked_opportunity, "signal_quality_score", None)
            signal.metrics["scan_tags"] = list(getattr(ranked_opportunity, "tags", []))
            ranked_metrics = getattr(ranked_opportunity, "metrics", {}) or {}
            signal.metrics.setdefault("scanner_timeframe", ranked_metrics.get("scanner_timeframe"))
            signal.metrics.setdefault("scan_inclusion_reasons", ranked_metrics.get("inclusion_reasons"))
        if news_features:
            signal.metrics["news_features"] = dict(news_features)

    def _strategy_signal_score(self, signal: TradeSignal) -> float:
        metrics = signal.metrics or {}
        return float(metrics.get("strategy_score") or signal.confidence_score or signal.strength or 0.0)

    def _entry_ml_score(self, signal: TradeSignal) -> float:
        ml_payload = (signal.metrics or {}).get("ml") or {}
        score = ml_payload.get("score")
        try:
            return float(score) if score is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _risk_quality_adjustment(self, signal: TradeSignal) -> float:
        metrics = signal.metrics or {}
        reward_risk = float(metrics.get("reward_risk_ratio") or 0.0)
        breakout_distance_atr = float(metrics.get("breakout_distance_atr") or 0.0)
        spread_pct = float(metrics.get("spread_pct") or 0.0)
        liquidity_score = float(signal.liquidity_score or 0.0)
        extended_move = bool(metrics.get("extended_move"))
        adjustment = 0.0
        adjustment += max(0.0, min(0.4, (reward_risk - 1.0) * 0.2))
        adjustment += max(-0.2, min(0.2, (0.4 - breakout_distance_atr) * 0.3))
        adjustment += liquidity_score * 0.15
        adjustment -= min(0.2, spread_pct * 10)
        if extended_move:
            adjustment -= 0.25
        return adjustment

    def _rank_buy_signals(self, buy_signals: list[TradeSignal]) -> list[TradeSignal]:
        ranked_signals: list[TradeSignal] = []
        for signal in buy_signals:
            strategy_score = self._strategy_signal_score(signal)
            entry_ml_score = self._entry_ml_score(signal)
            risk_quality_adjustment = self._risk_quality_adjustment(signal)
            combined_score = strategy_score + entry_ml_score + risk_quality_adjustment
            if signal.metrics is None:
                signal.metrics = {}
            signal.metrics["buy_ranking"] = {
                "strategy_score": strategy_score,
                "entry_ml_score": entry_ml_score,
                "risk_quality_adjustment": risk_quality_adjustment,
                "combined_score": combined_score,
            }
            ranked_signals.append(signal)
        return sorted(
            ranked_signals,
            key=lambda item: ((item.metrics or {}).get("buy_ranking") or {}).get("combined_score", 0.0),
            reverse=True,
        )

    def _estimated_entry_notional(self, signal: TradeSignal, *, equity: float) -> float:
        price = float(signal.entry_price or signal.price or 0.0)
        if price <= 0:
            return 0.0
        reward_risk = float((signal.metrics or {}).get("reward_risk_ratio") or 1.0)
        symbol_cap = equity * self.settings.max_symbol_allocation_pct
        risk_budget = equity * self.settings.risk_per_trade_pct
        stop_price = signal.stop_price
        if stop_price is not None and price > stop_price:
            stop_distance = price - float(stop_price)
            if stop_distance > 0:
                risk_budget = min(risk_budget / stop_distance * price, symbol_cap)
        notional_cap = min(
            self.settings.max_position_notional,
            self.settings.effective_max_position_notional,
            symbol_cap,
            risk_budget if risk_budget > 0 else symbol_cap,
        )
        if reward_risk < 1.0:
            notional_cap *= 0.5
        return max(0.0, notional_cap)

    def _select_ranked_buy_signals(self, buy_signals: list[TradeSignal]) -> list[TradeSignal]:
        equity = float(self.risk_manager.get_account_snapshot()["equity"])
        selected: list[TradeSignal] = []
        selected_symbols: set[str] = set()
        projected_symbol_exposure = {
            symbol: abs(self.portfolio.position_market_value(symbol) or 0.0)
            for symbol in self.portfolio.positions
        }
        projected_class_exposure = dict(self.portfolio.exposure_by_asset_class())
        available_slots = max(0, self.settings.max_concurrent_positions - len(self.portfolio.positions))
        new_slot_count = 0
        active_symbol_cooldowns = {
            item["symbol"]
            for item in self.risk_manager.get_active_cooldowns().get("stop_out_symbols", [])
        }

        for signal in self._rank_buy_signals(buy_signals):
            ranking = dict((signal.metrics or {}).get("buy_ranking") or {})
            symbol = signal.symbol
            asset_key = signal.asset_class.value
            estimated_notional = self._estimated_entry_notional(signal, equity=equity)
            ranking["estimated_notional"] = estimated_notional
            selection_rule = "selected_by_rank"
            selection_reason = "Candidate selected."
            consumes_new_slot = symbol not in self.portfolio.positions

            if symbol in selected_symbols:
                selection_rule = "duplicate_same_cycle"
                selection_reason = "Duplicate same-cycle entry blocked."
            elif symbol in active_symbol_cooldowns:
                selection_rule = "cooldown_active"
                selection_reason = "Recent stop-out cooldown is active."
            elif consumes_new_slot and new_slot_count >= available_slots:
                selection_rule = "portfolio_exposure_limit"
                selection_reason = "Portfolio max concurrent position limit reached for this cycle."
            else:
                symbol_cap = equity * self.settings.max_symbol_allocation_pct
                class_cap = equity * self.settings.max_asset_class_allocation_pct.get(
                    asset_key,
                    self.settings.max_symbol_allocation_pct,
                )
                projected_symbol_total = projected_symbol_exposure.get(symbol, 0.0) + estimated_notional
                projected_class_total = projected_class_exposure.get(asset_key, 0.0) + estimated_notional
                if projected_symbol_total > symbol_cap:
                    selection_rule = "portfolio_exposure_limit"
                    selection_reason = "Per-symbol allocation cap would be exceeded."
                elif projected_class_total > class_cap:
                    selection_rule = "portfolio_exposure_limit"
                    selection_reason = "Per-asset-class allocation cap would be exceeded."
                else:
                    selected.append(signal)
                    selected_symbols.add(symbol)
                    projected_symbol_exposure[symbol] = projected_symbol_total
                    projected_class_exposure[asset_key] = projected_class_total
                    if consumes_new_slot:
                        new_slot_count += 1

            ranking["selection_rule"] = selection_rule
            ranking["selection_reason"] = selection_reason
            signal.metrics["buy_ranking"] = ranking
        return selected

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
                order_intent=signal.order_intent,
                reduce_only=signal.reduce_only,
                exit_fraction=signal.exit_fraction,
                exit_stage=signal.exit_stage,
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
            order_intent=signal.order_intent,
            reduce_only=signal.reduce_only,
            exit_fraction=signal.exit_fraction,
            exit_stage=signal.exit_stage,
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
            self._latest_dust_resolution = None
            self._last_submitted_order = None
            self._process_started_at = datetime.datetime.now(datetime.timezone.utc)
            self._session_order_ids = set()
            self._broker_status_baseline_synced = False
            self._order_status_memory = self._load_broker_order_status_memory()
            self._latest_broker_order_status_updates = []
            self._summary_dedupe_suppressed = 0
            self._broker_status_dedupe_suppressed = 0
            self._recent_summary_fingerprints = {}
            self._last_notification_ids = []
            self._last_asset_class_run_at = {}
            self._last_cadence_diagnostics = {}
        self._release_process_lock()


_auto_trader: Optional[AutoTrader] = None


def get_auto_trader() -> AutoTrader:
    global _auto_trader
    from app.services.runtime import get_runtime

    _auto_trader = get_runtime().get_auto_trader()
    return _auto_trader
