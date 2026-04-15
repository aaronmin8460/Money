from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from app.config.settings import Settings, get_settings
from app.db.models import RankedOpportunityRecord, ScannerRun
from app.db.session import SessionLocal
from app.domain.models import AssetClass, AssetMetadata, NormalizedMarketSnapshot, RankedOpportunity
from app.monitoring.logger import get_logger
from app.services.asset_catalog import AssetCatalogService
from app.services.market_data import MarketDataService, canonicalize_symbol, infer_asset_class, normalize_asset_class

logger = get_logger("scanner")


@dataclass
class ScanResult:
    generated_at: datetime
    asset_class: str | None
    scanned_count: int
    opportunities: list[RankedOpportunity]
    top_gainers: list[RankedOpportunity]
    top_losers: list[RankedOpportunity]
    unusual_volume: list[RankedOpportunity]
    breakouts: list[RankedOpportunity]
    pullbacks: list[RankedOpportunity]
    volatility: list[RankedOpportunity]
    momentum: list[RankedOpportunity]
    regime_status: dict[str, int]
    errors: list[dict[str, str]]
    symbol_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    prefilter_counts: dict[str, int] = field(default_factory=dict)
    final_evaluation_counts: dict[str, int] = field(default_factory=dict)
    timeframes_by_asset_class: dict[str, dict[str, Any]] = field(default_factory=dict)
    symbol_inclusion_reasons: dict[str, list[str]] = field(default_factory=dict)
    selection_diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "asset_class": self.asset_class,
            "scanned_count": self.scanned_count,
            "opportunities": [item.to_dict() for item in self.opportunities],
            "top_gainers": [item.to_dict() for item in self.top_gainers],
            "top_losers": [item.to_dict() for item in self.top_losers],
            "unusual_volume": [item.to_dict() for item in self.unusual_volume],
            "breakouts": [item.to_dict() for item in self.breakouts],
            "pullbacks": [item.to_dict() for item in self.pullbacks],
            "volatility": [item.to_dict() for item in self.volatility],
            "momentum": [item.to_dict() for item in self.momentum],
            "regime_status": self.regime_status,
            "errors": self.errors,
            "symbol_snapshots": self.symbol_snapshots,
            "prefilter_counts": self.prefilter_counts,
            "final_evaluation_counts": self.final_evaluation_counts,
            "timeframes_by_asset_class": self.timeframes_by_asset_class,
            "symbol_inclusion_reasons": self.symbol_inclusion_reasons,
            "selection_diagnostics": self.selection_diagnostics,
        }


@dataclass
class PreparedScanAsset:
    asset: AssetMetadata
    inclusion_reasons: list[str] = field(default_factory=list)
    snapshot: NormalizedMarketSnapshot | None = None
    bars: pd.DataFrame | None = None
    prefilter_metrics: dict[str, Any] = field(default_factory=dict)
    prefilter_score: float = 0.0


@dataclass
class ScanSelectionPlan:
    assets: list[PreparedScanAsset]
    errors: list[dict[str, str]] = field(default_factory=list)
    prefilter_counts: dict[str, int] = field(default_factory=dict)
    final_evaluation_counts: dict[str, int] = field(default_factory=dict)
    timeframes_by_asset_class: dict[str, dict[str, Any]] = field(default_factory=dict)
    symbol_inclusion_reasons: dict[str, list[str]] = field(default_factory=dict)
    selection_diagnostics: dict[str, Any] = field(default_factory=dict)


class ScannerService:
    def __init__(
        self,
        asset_catalog: AssetCatalogService,
        market_data_service: MarketDataService,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.asset_catalog = asset_catalog
        self.market_data_service = market_data_service

    def scan(
        self,
        asset_class: AssetClass | str | None = None,
        symbols: list[str] | None = None,
        limit: int = 20,
        required_symbols: list[str] | None = None,
        inclusion_reasons: dict[str, list[str]] | None = None,
    ) -> ScanResult:
        started_at = datetime.utcnow()
        selection_plan = self._select_assets(
            asset_class,
            symbols,
            required_symbols=required_symbols,
            inclusion_reasons=inclusion_reasons,
        )
        opportunities: list[RankedOpportunity] = []
        errors: list[dict[str, str]] = list(selection_plan.errors)
        symbol_snapshots: dict[str, dict[str, Any]] = {}

        for prepared_asset in selection_plan.assets:
            asset = prepared_asset.asset
            try:
                normalized_snapshot = prepared_asset.snapshot or self.market_data_service.get_normalized_snapshot(
                    asset.symbol,
                    asset.asset_class,
                )
                symbol_snapshots[asset.symbol] = normalized_snapshot.to_dict()
                bars = prepared_asset.bars if prepared_asset.bars is not None else self.market_data_service.fetch_bars(
                    asset.symbol,
                    asset_class=asset.asset_class,
                    timeframe=self.settings.scanner_timeframe_for_asset_class(asset.asset_class),
                    limit=max(30, self.settings.lookback_bars_for_asset_class(asset.asset_class)),
                )
                opportunity = self._analyze_asset(
                    asset,
                    bars,
                    normalized_snapshot,
                    inclusion_reasons=prepared_asset.inclusion_reasons,
                    prefilter_metrics=prepared_asset.prefilter_metrics,
                    prefilter_score=prepared_asset.prefilter_score,
                )
                if opportunity is not None:
                    opportunities.append(opportunity)
            except Exception as exc:
                logger.warning("Scanner failed for %s: %s", asset.symbol, exc)
                errors.append({"symbol": asset.symbol, "error": str(exc)})

        result = self._build_result(
            started_at,
            asset_class,
            selection_plan.assets,
            opportunities,
            errors,
            limit,
            symbol_snapshots=symbol_snapshots,
            prefilter_counts=selection_plan.prefilter_counts,
            final_evaluation_counts=selection_plan.final_evaluation_counts,
            timeframes_by_asset_class=selection_plan.timeframes_by_asset_class,
            symbol_inclusion_reasons=selection_plan.symbol_inclusion_reasons,
            selection_diagnostics=selection_plan.selection_diagnostics,
        )
        self._persist_scan(result)
        return result

    def _select_assets(
        self,
        asset_class: AssetClass | str | None,
        symbols: list[str] | None,
        *,
        required_symbols: list[str] | None = None,
        inclusion_reasons: dict[str, list[str]] | None = None,
    ) -> ScanSelectionPlan:
        resolved_asset_class = normalize_asset_class(asset_class)
        reason_map = {
            canonicalize_symbol(symbol): list(reasons)
            for symbol, reasons in (inclusion_reasons or {}).items()
        }
        explicit_symbols = [self._canonical_symbol(symbol, resolved_asset_class) for symbol in symbols or []]
        required_symbols = [self._canonical_symbol(symbol, resolved_asset_class) for symbol in required_symbols or []]

        if explicit_symbols:
            ordered_symbols = list(dict.fromkeys(explicit_symbols + required_symbols))
            timeframes: dict[str, dict[str, Any]] = {}
            inclusion_by_symbol: dict[str, list[str]] = {}
            asset_inputs: list[tuple[AssetMetadata, list[str]]] = []
            for symbol in ordered_symbols:
                base_reasons = []
                if symbol in explicit_symbols:
                    base_reasons.append("explicit_symbol_request")
                base_reasons.extend(reason_map.get(symbol, []))
                asset_inputs.append(
                    (
                        self._resolve_asset(symbol, resolved_asset_class),
                        self._dedupe_reasons(base_reasons),
                    )
                )
            prepared_assets, errors = self._prepare_assets_for_scan(asset_inputs)
            for prepared_asset in prepared_assets:
                inclusion_by_symbol[prepared_asset.asset.symbol] = list(prepared_asset.inclusion_reasons)
                timeframes[prepared_asset.asset.asset_class.value] = self._timeframe_details(prepared_asset.asset.asset_class)
            return ScanSelectionPlan(
                assets=prepared_assets,
                errors=errors,
                prefilter_counts={},
                final_evaluation_counts={
                    asset_class_key: count
                    for asset_class_key, count in self._count_assets_by_class(prepared_assets).items()
                },
                timeframes_by_asset_class=timeframes,
                symbol_inclusion_reasons=inclusion_by_symbol,
                selection_diagnostics={
                    "selection_mode": "explicit_symbols",
                    "requested_symbols": ordered_symbols,
                },
            )

        universe = self.asset_catalog.get_scan_universe(asset_class)
        grouped: dict[AssetClass, list[AssetMetadata]] = {}
        for candidate in universe:
            grouped.setdefault(candidate.asset_class, []).append(candidate)

        forced_symbols_by_class: dict[AssetClass, dict[str, list[str]]] = {}
        for symbol in required_symbols:
            forced_asset = self._resolve_asset(symbol, resolved_asset_class)
            if not self._matches_asset_class_filter(forced_asset, resolved_asset_class):
                continue
            forced_symbols_by_class.setdefault(forced_asset.asset_class, {}).setdefault(forced_asset.symbol, []).extend(
                self._dedupe_reasons(["required_monitoring_symbol", *reason_map.get(symbol, [])])
            )

        for symbol in self.settings.included_symbols:
            included_asset = self._resolve_asset(symbol, resolved_asset_class)
            if not self._matches_asset_class_filter(included_asset, resolved_asset_class):
                continue
            forced_symbols_by_class.setdefault(included_asset.asset_class, {}).setdefault(
                included_asset.symbol,
                [],
            ).append("configured_include")

        if self.settings.scan_universe_mode.lower() == "major" and not self.settings.scan_symbol_allowlist:
            for symbol in self.settings.major_equity_symbols + self.settings.major_crypto_symbols:
                major_asset = self._resolve_asset(symbol, resolved_asset_class)
                if not self._matches_asset_class_filter(major_asset, resolved_asset_class):
                    continue
                forced_symbols_by_class.setdefault(major_asset.asset_class, {}).setdefault(
                    major_asset.symbol,
                    [],
                ).append("major_mode")

        ordered_assets: list[PreparedScanAsset] = []
        errors: list[dict[str, str]] = []
        prefilter_counts: dict[str, int] = {}
        final_evaluation_counts: dict[str, int] = {}
        timeframes_by_asset_class: dict[str, dict[str, Any]] = {}
        symbol_inclusion_reasons: dict[str, list[str]] = {}
        selection_diagnostics: dict[str, Any] = {}

        for current_asset_class in sorted(
            set(grouped.keys()).union(forced_symbols_by_class.keys()),
            key=lambda item: item.value,
        ):
            class_universe = grouped.get(current_asset_class, [])
            class_forced_symbols = {
                symbol: self._dedupe_reasons(reasons)
                for symbol, reasons in forced_symbols_by_class.get(current_asset_class, {}).items()
            }
            timeframes_by_asset_class[current_asset_class.value] = self._timeframe_details(current_asset_class)

            forced_inputs = [
                (self._resolve_asset(symbol, current_asset_class), reasons)
                for symbol, reasons in class_forced_symbols.items()
            ]
            prepared_forced_assets, forced_errors = self._prepare_assets_for_scan(forced_inputs)
            errors.extend(forced_errors)

            forced_symbol_set = set(class_forced_symbols.keys()).union({item.asset.symbol for item in prepared_forced_assets})
            broader_inputs: list[tuple[AssetMetadata, list[str]]] = []
            for asset_candidate in class_universe:
                if asset_candidate.symbol in forced_symbol_set:
                    continue
                broader_inputs.append((asset_candidate, ["ranked_universe_candidate"]))
            scored_broader_assets, broader_errors = self._prepare_assets_for_scan(broader_inputs)
            errors.extend(broader_errors)

            scored_broader_assets.sort(
                key=lambda item: (item.prefilter_score, item.asset.symbol),
                reverse=True,
            )
            prefilter_limit = self.settings.universe_prefilter_limit_for_asset_class(current_asset_class)
            final_limit = self.settings.final_evaluation_limit_for_asset_class(current_asset_class)
            prefilter_pool = scored_broader_assets[:prefilter_limit]
            final_ranked_assets = prefilter_pool[:final_limit]
            for prepared_asset in final_ranked_assets:
                prepared_asset.inclusion_reasons = self._dedupe_reasons(
                    [*prepared_asset.inclusion_reasons, "prefilter_top_ranked", "final_evaluation_ranked"]
                )

            class_selected_assets = self._dedupe_prepared_assets(prepared_forced_assets + final_ranked_assets)
            ordered_assets.extend(class_selected_assets)

            prefilter_counts[current_asset_class.value] = len(prefilter_pool)
            final_evaluation_counts[current_asset_class.value] = len(class_selected_assets)
            selection_diagnostics[current_asset_class.value] = {
                "eligible_universe_count": len(class_universe),
                "required_symbol_count": len(prepared_forced_assets),
                "prefilter_limit": prefilter_limit,
                "prefilter_ranked_count": len(prefilter_pool),
                "final_evaluation_limit": final_limit,
                "final_selected_count": len(class_selected_assets),
                "scanner_timeframe": timeframes_by_asset_class[current_asset_class.value]["scanner_timeframe"],
                "lookback_bars": timeframes_by_asset_class[current_asset_class.value]["lookback_bars"],
            }
            for prepared_asset in class_selected_assets:
                symbol_inclusion_reasons[prepared_asset.asset.symbol] = list(prepared_asset.inclusion_reasons)

        return ScanSelectionPlan(
            assets=ordered_assets,
            errors=errors,
            prefilter_counts=prefilter_counts,
            final_evaluation_counts=final_evaluation_counts,
            timeframes_by_asset_class=timeframes_by_asset_class,
            symbol_inclusion_reasons=symbol_inclusion_reasons,
            selection_diagnostics=selection_diagnostics,
        )

    def _analyze_asset(
        self,
        asset: AssetMetadata,
        bars: pd.DataFrame,
        snapshot: NormalizedMarketSnapshot,
        *,
        inclusion_reasons: list[str] | None = None,
        prefilter_metrics: dict[str, Any] | None = None,
        prefilter_score: float | None = None,
    ) -> RankedOpportunity | None:
        stats = self._compute_analysis_stats(asset, bars, snapshot)
        if stats is None:
            return None

        if stats["last_price"] < self.settings.min_price:
            return None
        if stats["dollar_volume"] < self.settings.min_dollar_volume and asset.asset_class != AssetClass.CRYPTO:
            return None

        tags: list[str] = []
        if stats["breakout_candidate"]:
            tags.append("breakout")
        if stats["pullback_candidate"]:
            tags.append("pullback")
        if stats["relative_volume"] >= 1.5:
            tags.append("unusual_volume")
        if stats["return_1"] > 0:
            tags.append("gainer")
        elif stats["return_1"] < 0:
            tags.append("loser")
        if asset.asset_class == AssetClass.CRYPTO:
            tags.append("twenty_four_seven")
        if snapshot.session_state:
            tags.append(snapshot.session_state)

        return RankedOpportunity(
            symbol=asset.symbol,
            asset_class=asset.asset_class,
            name=asset.name,
            last_price=stats["last_price"],
            price_change_pct=stats["price_change_pct"],
            momentum_score=stats["momentum_score"],
            volatility_score=stats["volatility_score"],
            liquidity_score=stats["liquidity_score"],
            spread_score=stats["spread_score"],
            tradability_score=stats["tradability_score"],
            signal_quality_score=stats["signal_quality_score"],
            regime_state=stats["regime_state"],
            tags=tags,
            reason="Momentum breakout candidate" if stats["breakout_candidate"] else (
                "Trend pullback candidate" if stats["pullback_candidate"] else "Ranked market opportunity"
            ),
            metrics={
                "atr_pct": stats["atr_pct"],
                "relative_volume": stats["relative_volume"],
                "dollar_volume": stats["dollar_volume"],
                "spread_pct": stats["spread_pct"],
                "last_close": stats["latest_close"],
                "scanner_timeframe": self.settings.scanner_timeframe_for_asset_class(asset.asset_class),
                "scanner_lookback_bars": self.settings.lookback_bars_for_asset_class(asset.asset_class),
                "prefilter_score": prefilter_score,
                "prefilter_metrics": dict(prefilter_metrics or {}),
                "inclusion_reasons": list(inclusion_reasons or []),
                "session_state": snapshot.session_state,
                "quote_available": snapshot.quote_available,
                "quote_stale": snapshot.quote_stale,
                "price_source_for_ranking": snapshot.price_source_used,
                "normalized_snapshot": snapshot.to_dict(),
            },
        )

    def _build_result(
        self,
        started_at: datetime,
        asset_class: AssetClass | str | None,
        selected_assets: list[AssetMetadata],
        opportunities: list[RankedOpportunity],
        errors: list[dict[str, str]],
        limit: int,
        *,
        symbol_snapshots: dict[str, dict[str, Any]] | None = None,
        prefilter_counts: dict[str, int] | None = None,
        final_evaluation_counts: dict[str, int] | None = None,
        timeframes_by_asset_class: dict[str, dict[str, Any]] | None = None,
        symbol_inclusion_reasons: dict[str, list[str]] | None = None,
        selection_diagnostics: dict[str, Any] | None = None,
    ) -> ScanResult:
        generated_at = datetime.utcnow()
        sorted_by_quality = sorted(opportunities, key=lambda item: item.signal_quality_score, reverse=True)
        regime_status: dict[str, int] = {}
        for item in opportunities:
            regime_status[item.regime_state] = regime_status.get(item.regime_state, 0) + 1

        return ScanResult(
            generated_at=generated_at,
            asset_class=(asset_class.value if isinstance(asset_class, AssetClass) else asset_class),
            scanned_count=len(selected_assets),
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
            symbol_snapshots=symbol_snapshots or {},
            prefilter_counts=prefilter_counts or {},
            final_evaluation_counts=final_evaluation_counts or {},
            timeframes_by_asset_class=timeframes_by_asset_class or {},
            symbol_inclusion_reasons=symbol_inclusion_reasons or {},
            selection_diagnostics=selection_diagnostics or {},
        )

    def _persist_scan(self, result: ScanResult) -> None:
        try:
            with SessionLocal() as session:
                run = ScannerRun(
                    started_at=result.generated_at,
                    completed_at=result.generated_at,
                    asset_class=result.asset_class,
                    symbols_scanned=result.scanned_count,
                    signals_generated=len(result.opportunities),
                    status="success" if not result.errors else "partial",
                    metadata_json=json.dumps(
                        {
                            "errors": result.errors,
                            "prefilter_counts": result.prefilter_counts,
                            "final_evaluation_counts": result.final_evaluation_counts,
                            "timeframes_by_asset_class": result.timeframes_by_asset_class,
                            "selection_diagnostics": result.selection_diagnostics,
                        },
                        default=str,
                    ),
                )
                session.add(run)
                session.flush()

                for opportunity in result.opportunities:
                    session.add(
                        RankedOpportunityRecord(
                            scanner_run_id=run.id,
                            symbol=opportunity.symbol,
                            asset_class=opportunity.asset_class.value,
                            name=opportunity.name,
                            last_price=opportunity.last_price,
                            price_change_pct=opportunity.price_change_pct,
                            momentum_score=opportunity.momentum_score,
                            volatility_score=opportunity.volatility_score,
                            liquidity_score=opportunity.liquidity_score,
                            spread_score=opportunity.spread_score,
                            tradability_score=opportunity.tradability_score,
                            signal_quality_score=opportunity.signal_quality_score,
                            regime_state=opportunity.regime_state,
                            tags=json.dumps(opportunity.tags),
                            reason=opportunity.reason,
                            metrics_json=json.dumps(opportunity.metrics, default=str),
                        )
                    )
                session.commit()
        except Exception as exc:
            logger.warning("Failed to persist scanner run: %s", exc)

    def _bounded_score(self, value: float, scale: float) -> float:
        if scale == 0:
            return 0.0
        normalized = 0.5 + (value * scale)
        return max(0.0, min(1.0, normalized))

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
        canonical_symbol = canonicalize_symbol(symbol, resolved_asset_class)
        return AssetMetadata(
            symbol=canonical_symbol,
            name=canonical_symbol,
            asset_class=resolved_asset_class,
            exchange="CRYPTO" if resolved_asset_class == AssetClass.CRYPTO else None,
            tradable=True,
            fractionable=resolved_asset_class in {AssetClass.ETF, AssetClass.CRYPTO},
            shortable=resolved_asset_class != AssetClass.CRYPTO,
            easy_to_borrow=resolved_asset_class != AssetClass.CRYPTO,
            marginable=resolved_asset_class != AssetClass.CRYPTO,
            attributes=["explicit_symbol_fallback"],
            raw={"source": "explicit_symbol_fallback"},
        )

    def _canonical_symbol(
        self,
        symbol: str,
        asset_class: AssetClass | str | None = None,
    ) -> str:
        resolved_asset_class = normalize_asset_class(asset_class)
        if resolved_asset_class == AssetClass.UNKNOWN:
            resolved_asset_class = infer_asset_class(symbol)
        return canonicalize_symbol(symbol, resolved_asset_class)

    def _prepare_assets_for_scan(
        self,
        asset_inputs: list[tuple[AssetMetadata, list[str]]],
    ) -> tuple[list[PreparedScanAsset], list[dict[str, str]]]:
        if not asset_inputs:
            return [], []

        snapshots: dict[str, NormalizedMarketSnapshot] = {}
        grouped: dict[AssetClass, list[AssetMetadata]] = {}
        for asset, _reasons in asset_inputs:
            grouped.setdefault(asset.asset_class, []).append(asset)

        for asset_class, assets in grouped.items():
            snapshots.update(self._batch_snapshots_for_scan(assets, asset_class))

        bars_cache: dict[tuple[str, str, str, int], pd.DataFrame] = {}
        prepared_assets: list[PreparedScanAsset] = []
        errors: list[dict[str, str]] = []
        for asset, reasons in asset_inputs:
            prepared_asset, error = self._prepare_asset_for_scan(
                asset,
                inclusion_reasons=reasons,
                prepared_snapshot=snapshots.get(asset.symbol),
                bars_cache=bars_cache,
            )
            if error is not None:
                errors.append(error)
                continue
            assert prepared_asset is not None
            prepared_assets.append(prepared_asset)
        return prepared_assets, errors

    def _batch_snapshots_for_scan(
        self,
        assets: list[AssetMetadata],
        asset_class: AssetClass,
    ) -> dict[str, NormalizedMarketSnapshot]:
        if not assets:
            return {}
        batch_snapshot = getattr(self.market_data_service, "batch_snapshot", None)
        if not callable(batch_snapshot):
            return {}
        symbols = [asset.symbol for asset in assets]
        try:
            payloads = batch_snapshot(symbols, asset_class)
        except Exception as exc:
            logger.warning(
                "Scanner batch snapshot failed",
                extra={"asset_class": asset_class.value, "symbol_count": len(symbols), "error": str(exc)},
            )
            return {}

        snapshots: dict[str, NormalizedMarketSnapshot] = {}
        for symbol, payload in (payloads or {}).items():
            canonical_symbol = canonicalize_symbol(symbol, asset_class)
            normalized = self._normalized_snapshot_from_batch_payload(canonical_symbol, asset_class, payload)
            if normalized is not None:
                snapshots[canonical_symbol] = normalized
        return snapshots

    def _normalized_snapshot_from_batch_payload(
        self,
        symbol: str,
        asset_class: AssetClass,
        payload: Any,
    ) -> NormalizedMarketSnapshot | None:
        if isinstance(payload, NormalizedMarketSnapshot):
            return payload
        if not isinstance(payload, dict):
            return None
        normalized_payload = payload.get("normalized") or payload.get("normalized_snapshot")
        if isinstance(normalized_payload, NormalizedMarketSnapshot):
            return normalized_payload
        if isinstance(normalized_payload, dict):
            return NormalizedMarketSnapshot.from_dict(normalized_payload)
        if "evaluation_price" in payload or "last_trade_price" in payload:
            candidate = dict(payload)
            candidate.setdefault("symbol", symbol)
            candidate.setdefault("asset_class", asset_class.value)
            return NormalizedMarketSnapshot.from_dict(candidate)
        return None

    def _prepare_asset_for_scan(
        self,
        asset: AssetMetadata,
        *,
        inclusion_reasons: list[str] | None = None,
        prepared_snapshot: NormalizedMarketSnapshot | None = None,
        bars_cache: dict[tuple[str, str, str, int], pd.DataFrame] | None = None,
    ) -> tuple[PreparedScanAsset | None, dict[str, str] | None]:
        try:
            snapshot = prepared_snapshot or self.market_data_service.get_normalized_snapshot(asset.symbol, asset.asset_class)
            timeframe = self.settings.scanner_timeframe_for_asset_class(asset.asset_class)
            limit = max(30, self.settings.lookback_bars_for_asset_class(asset.asset_class))
            cache_key = (asset.symbol, asset.asset_class.value, timeframe, limit)
            bars = bars_cache.get(cache_key) if bars_cache is not None else None
            if bars is None:
                bars = self.market_data_service.fetch_bars(
                    asset.symbol,
                    asset_class=asset.asset_class,
                    timeframe=timeframe,
                    limit=limit,
                )
                if bars_cache is not None:
                    bars_cache[cache_key] = bars
            prefilter_metrics = self._compute_prefilter_metrics(asset, bars, snapshot)
            prefilter_metrics["provider_data_quality"] = {
                "source": snapshot.source,
                "quote_available": snapshot.quote_available,
                "quote_stale": snapshot.quote_stale,
                "fallback_pricing_used": snapshot.fallback_pricing_used,
                "price_source_used": snapshot.price_source_used,
                "missing_fields": [
                    field
                    for field, missing in {
                        "bid_price": snapshot.bid_price is None,
                        "ask_price": snapshot.ask_price is None,
                        "spread_pct": snapshot.spread_pct is None,
                        "source_timestamp": snapshot.source_timestamp is None,
                    }.items()
                    if missing
                ],
            }
            prefilter_score = self._prefilter_score(prefilter_metrics)
            return (
                PreparedScanAsset(
                    asset=asset,
                    inclusion_reasons=self._dedupe_reasons(inclusion_reasons or []),
                    snapshot=snapshot,
                    bars=bars,
                    prefilter_metrics=prefilter_metrics,
                    prefilter_score=prefilter_score,
                ),
                None,
            )
        except Exception as exc:
            logger.warning("Scanner prefilter failed for %s: %s", asset.symbol, exc)
            return None, {"symbol": asset.symbol, "error": str(exc)}

    def _compute_analysis_stats(
        self,
        asset: AssetMetadata,
        bars: pd.DataFrame,
        snapshot: NormalizedMarketSnapshot,
    ) -> dict[str, Any] | None:
        if bars.empty or len(bars) < 5:
            return None

        df = bars.copy()
        df["return_1"] = df["Close"].pct_change(1)
        df["return_5"] = df["Close"].pct_change(min(5, len(df) - 1))
        df["return_10"] = df["Close"].pct_change(min(10, len(df) - 1))
        df["sma_fast"] = df["Close"].rolling(window=min(10, len(df))).mean()
        df["sma_slow"] = df["Close"].rolling(window=min(20, len(df))).mean()
        df["high_low"] = df["High"] - df["Low"]
        df["high_close"] = (df["High"] - df["Close"].shift(1)).abs()
        df["low_close"] = (df["Low"] - df["Close"].shift(1)).abs()
        df["true_range"] = df[["high_low", "high_close", "low_close"]].max(axis=1)
        df["atr"] = df["true_range"].rolling(window=min(14, len(df)), min_periods=1).mean()
        df["avg_volume"] = df["Volume"].rolling(window=min(20, len(df)), min_periods=1).mean()
        df["breakout_level"] = df["High"].rolling(window=min(10, len(df)), min_periods=1).max().shift(1)
        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest

        last_price = float(snapshot.evaluation_price or snapshot.last_trade_price or latest["Close"])
        spread_pct = float(snapshot.spread_pct or 0.0)
        avg_volume = float(latest["avg_volume"] or 0.0)
        dollar_volume = avg_volume * last_price
        relative_volume = float(latest["Volume"] / avg_volume) if avg_volume > 0 else 0.0
        atr_pct = float(latest["atr"] / last_price) if last_price > 0 and latest["atr"] else 0.0
        return_1 = float(latest.get("return_1") or 0.0)
        return_5 = float(latest.get("return_5") or 0.0)
        return_10 = float(latest.get("return_10") or 0.0)
        momentum_score = self._bounded_score((return_1 * 0.4) + (return_5 * 0.4) + (return_10 * 0.2), scale=12.0)
        liquidity_score = self._bounded_score(
            math.log10(max(dollar_volume, 1.0)) - math.log10(max(self.settings.min_dollar_volume, 1.0)),
            scale=1.0,
        )
        spread_score = 1.0 if spread_pct <= 0 else max(0.0, 1.0 - (spread_pct / self.settings.max_spread_pct))
        volatility_score = self._bounded_score(min(atr_pct, 0.20), scale=8.0)
        movement_score = self._bounded_score(abs(return_5) + (abs(return_10) * 0.5), scale=6.0)
        relative_volume_score = self._bounded_score(relative_volume - 1.0, scale=0.5)

        trend_up = bool(
            pd.notna(latest["sma_fast"])
            and pd.notna(latest["sma_slow"])
            and latest["sma_fast"] >= latest["sma_slow"]
        )
        breakout_candidate = bool(
            pd.notna(latest["breakout_level"])
            and last_price >= float(latest["breakout_level"])
            and relative_volume >= 1.0
        )
        pullback_candidate = bool(
            trend_up
            and pd.notna(latest["sma_fast"])
            and abs(last_price - float(latest["sma_fast"])) / max(last_price, 1e-9) <= 0.025
        )
        regime_state = "bullish" if trend_up else "bearish"
        tradability_score = max(0.0, min(1.0, (liquidity_score * 0.6) + (spread_score * 0.4)))
        signal_quality_score = max(
            0.0,
            min(
                1.0,
                momentum_score * 0.30
                + volatility_score * 0.12
                + liquidity_score * 0.23
                + spread_score * 0.15
                + relative_volume_score * 0.10
                + movement_score * 0.10
                + (0.08 if breakout_candidate or pullback_candidate else 0.0),
            ),
        )

        previous_close = float(previous["Close"]) if float(previous["Close"]) > 0 else None
        price_change_pct = None
        if previous_close:
            price_change_pct = float((last_price - previous_close) / previous_close)

        return {
            "last_price": last_price,
            "price_change_pct": price_change_pct,
            "momentum_score": momentum_score,
            "volatility_score": volatility_score,
            "liquidity_score": liquidity_score,
            "spread_score": spread_score,
            "tradability_score": tradability_score,
            "signal_quality_score": signal_quality_score,
            "regime_state": regime_state,
            "atr_pct": atr_pct,
            "relative_volume": relative_volume,
            "dollar_volume": dollar_volume,
            "spread_pct": spread_pct,
            "latest_close": float(latest["Close"]),
            "previous_close": previous_close,
            "return_1": return_1,
            "return_5": return_5,
            "return_10": return_10,
            "movement_score": movement_score,
            "relative_volume_score": relative_volume_score,
            "trend_up": trend_up,
            "breakout_candidate": breakout_candidate,
            "pullback_candidate": pullback_candidate,
        }

    def _compute_prefilter_metrics(
        self,
        asset: AssetMetadata,
        bars: pd.DataFrame,
        snapshot: NormalizedMarketSnapshot,
    ) -> dict[str, Any]:
        stats = self._compute_analysis_stats(asset, bars, snapshot)
        if stats is None:
            return {
                "symbol": asset.symbol,
                "asset_class": asset.asset_class.value,
                "scanner_timeframe": self.settings.scanner_timeframe_for_asset_class(asset.asset_class),
                "lookback_bars": self.settings.lookback_bars_for_asset_class(asset.asset_class),
            }
        return {
            "symbol": asset.symbol,
            "asset_class": asset.asset_class.value,
            "dollar_volume": stats["dollar_volume"],
            "spread_score": stats["spread_score"],
            "volatility_score": stats["volatility_score"],
            "relative_volume": stats["relative_volume"],
            "relative_volume_score": stats["relative_volume_score"],
            "movement_score": stats["movement_score"],
            "atr_pct": stats["atr_pct"],
            "price_change_pct": stats["price_change_pct"],
            "scanner_timeframe": self.settings.scanner_timeframe_for_asset_class(asset.asset_class),
            "lookback_bars": self.settings.lookback_bars_for_asset_class(asset.asset_class),
        }

    def _prefilter_score(self, metrics: dict[str, Any]) -> float:
        if not metrics:
            return 0.0
        liquidity_score = self._bounded_score(
            math.log10(max(float(metrics.get("dollar_volume") or 1.0), 1.0))
            - math.log10(max(self.settings.min_dollar_volume, 1.0)),
            scale=1.0,
        )
        spread_score = float(metrics.get("spread_score") or 0.0)
        volatility_score = float(metrics.get("volatility_score") or 0.0)
        relative_volume_score = float(metrics.get("relative_volume_score") or 0.0)
        movement_score = float(metrics.get("movement_score") or 0.0)
        return max(
            0.0,
            min(
                1.0,
                liquidity_score * 0.35
                + spread_score * 0.20
                + volatility_score * 0.15
                + relative_volume_score * 0.15
                + movement_score * 0.15,
            ),
        )

    def _timeframe_details(self, asset_class: AssetClass) -> dict[str, Any]:
        return {
            "scanner_timeframe": self.settings.scanner_timeframe_for_asset_class(asset_class),
            "entry_timeframe": self.settings.entry_timeframe_for_asset_class(asset_class),
            "regime_timeframe": self.settings.regime_timeframe_for_asset_class(asset_class),
            "lookback_bars": self.settings.lookback_bars_for_asset_class(asset_class),
        }

    def _matches_asset_class_filter(
        self,
        asset: AssetMetadata,
        requested_asset_class: AssetClass,
    ) -> bool:
        return requested_asset_class == AssetClass.UNKNOWN or asset.asset_class == requested_asset_class

    def _count_assets_by_class(self, assets: list[PreparedScanAsset]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for asset in assets:
            key = asset.asset.asset_class.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _dedupe_prepared_assets(self, assets: list[PreparedScanAsset]) -> list[PreparedScanAsset]:
        deduped: list[PreparedScanAsset] = []
        seen: set[str] = set()
        for asset in assets:
            if asset.asset.symbol in seen:
                continue
            seen.add(asset.asset.symbol)
            deduped.append(asset)
        return deduped

    def _dedupe_reasons(self, reasons: list[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for reason in reasons:
            normalized = str(reason).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered
