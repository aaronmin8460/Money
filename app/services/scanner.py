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
from app.services.market_data import MarketDataService

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
        }


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
    ) -> ScanResult:
        started_at = datetime.utcnow()
        selected_assets = self._select_assets(asset_class, symbols)
        opportunities: list[RankedOpportunity] = []
        errors: list[dict[str, str]] = []
        symbol_snapshots: dict[str, dict[str, Any]] = {}

        for asset in selected_assets:
            try:
                normalized_snapshot = self.market_data_service.get_normalized_snapshot(asset.symbol, asset.asset_class)
                symbol_snapshots[asset.symbol] = normalized_snapshot.to_dict()
                bars = self.market_data_service.fetch_bars(
                    asset.symbol,
                    asset_class=asset.asset_class,
                    timeframe=self.settings.default_timeframe,
                    limit=max(30, self.settings.scanner_limit_per_asset_class),
                )
                opportunity = self._analyze_asset(asset, bars, normalized_snapshot)
                if opportunity is not None:
                    opportunities.append(opportunity)
            except Exception as exc:
                logger.warning("Scanner failed for %s: %s", asset.symbol, exc)
                errors.append({"symbol": asset.symbol, "error": str(exc)})

        result = self._build_result(
            started_at,
            asset_class,
            selected_assets,
            opportunities,
            errors,
            limit,
            symbol_snapshots=symbol_snapshots,
        )
        self._persist_scan(result)
        return result

    def _select_assets(
        self,
        asset_class: AssetClass | str | None,
        symbols: list[str] | None,
    ) -> list[AssetMetadata]:
        if symbols:
            universe = []
            for symbol in symbols:
                asset = self.asset_catalog.get_asset(symbol)
                if asset is not None:
                    universe.append(asset)
            return universe

        universe = self.asset_catalog.get_scan_universe(asset_class)
        per_class_limit = max(1, self.settings.scanner_limit_per_asset_class)
        grouped: dict[AssetClass, list[AssetMetadata]] = {}
        for asset in universe:
            grouped.setdefault(asset.asset_class, []).append(asset)

        selected: list[AssetMetadata] = []
        for assets in grouped.values():
            selected.extend(assets[:per_class_limit])
        return selected

    def _analyze_asset(
        self,
        asset: AssetMetadata,
        bars: pd.DataFrame,
        snapshot: NormalizedMarketSnapshot,
    ) -> RankedOpportunity | None:
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
        df["avg_volume"] = df["Volume"].rolling(window=min(10, len(df)), min_periods=1).mean()
        df["breakout_level"] = df["High"].rolling(window=min(10, len(df)), min_periods=1).max().shift(1)
        latest = df.iloc[-1]
        previous = df.iloc[-2] if len(df) > 1 else latest

        last_price = float(snapshot.evaluation_price or snapshot.last_trade_price or latest["Close"])
        spread_pct = snapshot.spread_pct or 0.0
        avg_volume = float(latest["avg_volume"] or 0.0)
        dollar_volume = avg_volume * last_price
        relative_volume = float(latest["Volume"] / avg_volume) if avg_volume > 0 else 0.0
        atr_pct = float(latest["atr"] / last_price) if last_price > 0 and latest["atr"] else 0.0
        momentum_score = self._bounded_score(
            ((latest.get("return_1") or 0.0) * 0.4)
            + ((latest.get("return_5") or 0.0) * 0.4)
            + ((latest.get("return_10") or 0.0) * 0.2),
            scale=12.0,
        )
        liquidity_score = self._bounded_score(
            math.log10(max(dollar_volume, 1.0)) - math.log10(max(self.settings.min_dollar_volume, 1.0)),
            scale=1.0,
        )
        spread_score = 1.0 if spread_pct <= 0 else max(0.0, 1.0 - (spread_pct / self.settings.max_spread_pct))
        volatility_score = self._bounded_score(atr_pct, scale=12.0)

        trend_up = bool(pd.notna(latest["sma_fast"]) and pd.notna(latest["sma_slow"]) and latest["sma_fast"] >= latest["sma_slow"])
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
                momentum_score * 0.35
                + volatility_score * 0.15
                + liquidity_score * 0.25
                + spread_score * 0.15
                + (0.1 if breakout_candidate or pullback_candidate else 0.0),
            ),
        )

        if last_price < self.settings.min_price:
            return None
        if dollar_volume < self.settings.min_dollar_volume and asset.asset_class != AssetClass.CRYPTO:
            return None

        tags: list[str] = []
        if breakout_candidate:
            tags.append("breakout")
        if pullback_candidate:
            tags.append("pullback")
        if relative_volume >= 1.5:
            tags.append("unusual_volume")
        if (latest.get("return_1") or 0.0) > 0:
            tags.append("gainer")
        elif (latest.get("return_1") or 0.0) < 0:
            tags.append("loser")
        if asset.asset_class == AssetClass.CRYPTO:
            tags.append("twenty_four_seven")
        if snapshot.session_state:
            tags.append(snapshot.session_state)

        return RankedOpportunity(
            symbol=asset.symbol,
            asset_class=asset.asset_class,
            name=asset.name,
            last_price=last_price,
            price_change_pct=float((last_price - float(previous["Close"])) / float(previous["Close"])) if float(previous["Close"]) > 0 else None,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            liquidity_score=liquidity_score,
            spread_score=spread_score,
            tradability_score=tradability_score,
            signal_quality_score=signal_quality_score,
            regime_state=regime_state,
            tags=tags,
            reason="Momentum breakout candidate" if breakout_candidate else (
                "Trend pullback candidate" if pullback_candidate else "Ranked market opportunity"
            ),
            metrics={
                "atr_pct": atr_pct,
                "relative_volume": relative_volume,
                "dollar_volume": dollar_volume,
                "spread_pct": spread_pct,
                "last_close": float(latest["Close"]),
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
                    metadata_json=json.dumps({"errors": result.errors}, default=str),
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
