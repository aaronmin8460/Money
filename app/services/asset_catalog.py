from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from app.config.settings import Settings, get_settings
from app.db.models import AssetCatalogEntry, AssetCatalogSyncRun
from app.db.session import SessionLocal
from app.domain.models import AssetClass, AssetMetadata
from app.monitoring.logger import get_logger
from app.services.broker import BrokerInterface
from app.services.market_data import canonicalize_symbol, normalize_asset_class

logger = get_logger("asset_catalog")


@dataclass
class AssetCatalogRefreshResult:
    asset_count: int
    refreshed_at: datetime
    cache_hit: bool
    source: str


class AssetCatalogService:
    def __init__(self, broker: BrokerInterface, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.broker = broker

    def ensure_fresh(self) -> AssetCatalogRefreshResult:
        with SessionLocal() as session:
            latest_run = (
                session.query(AssetCatalogSyncRun)
                .order_by(AssetCatalogSyncRun.started_at.desc())
                .first()
            )
            if latest_run and latest_run.completed_at:
                age = datetime.utcnow() - latest_run.completed_at
                if age < timedelta(minutes=self.settings.universe_refresh_minutes):
                    asset_count = session.query(func.count(AssetCatalogEntry.id)).scalar() or 0
                    return AssetCatalogRefreshResult(
                        asset_count=asset_count,
                        refreshed_at=latest_run.completed_at,
                        cache_hit=True,
                        source=latest_run.source,
                    )
        return self.refresh(force=True)

    def refresh(self, force: bool = False) -> AssetCatalogRefreshResult:
        if not force:
            cached = self.ensure_fresh()
            if cached.cache_hit:
                return cached

        started_at = datetime.utcnow()
        source = type(self.broker).__name__
        sync_id: int | None = None
        with SessionLocal() as session:
            sync_run = AssetCatalogSyncRun(
                started_at=started_at,
                source=source,
                cache_hit=False,
                status="running",
            )
            session.add(sync_run)
            session.commit()
            sync_id = sync_run.id

        try:
            assets = self.broker.list_assets()
            with SessionLocal() as session:
                for asset in assets:
                    serialized_attributes = json.dumps(asset.attributes)
                    raw_payload = json.dumps(asset.raw, default=str)
                    row = (
                        session.query(AssetCatalogEntry)
                        .filter(AssetCatalogEntry.symbol == asset.symbol)
                        .one_or_none()
                    )
                    if row is None:
                        row = AssetCatalogEntry(symbol=asset.symbol, name=asset.name)
                        session.add(row)
                    row.name = asset.name
                    row.asset_class = asset.asset_class.value
                    row.exchange = asset.exchange
                    row.status = asset.status
                    row.tradable = asset.tradable
                    row.fractionable = asset.fractionable
                    row.shortable = asset.shortable
                    row.easy_to_borrow = asset.easy_to_borrow
                    row.marginable = asset.marginable
                    row.attributes = serialized_attributes
                    row.source = source
                    row.synced_at = datetime.utcnow()
                    row.raw_payload = raw_payload

                sync_run = session.query(AssetCatalogSyncRun).filter(AssetCatalogSyncRun.id == sync_id).one()
                sync_run.completed_at = datetime.utcnow()
                sync_run.asset_count = len(assets)
                sync_run.status = "success"
                session.commit()

            logger.info("Asset catalog synced", extra={"asset_count": len(assets), "source": source})
            return AssetCatalogRefreshResult(
                asset_count=len(assets),
                refreshed_at=datetime.utcnow(),
                cache_hit=False,
                source=source,
            )
        except Exception as exc:
            logger.error("Asset catalog sync failed: %s", exc)
            with SessionLocal() as session:
                sync_run = session.query(AssetCatalogSyncRun).filter(AssetCatalogSyncRun.id == sync_id).one_or_none()
                if sync_run is not None:
                    sync_run.completed_at = datetime.utcnow()
                    sync_run.status = "error"
                    sync_run.error_message = str(exc)
                    session.commit()
            raise

    def _row_to_asset(self, row: AssetCatalogEntry) -> AssetMetadata:
        raw_attributes = json.loads(row.attributes) if row.attributes else []
        raw_payload = json.loads(row.raw_payload) if row.raw_payload else {}
        return AssetMetadata(
            symbol=row.symbol,
            name=row.name,
            asset_class=normalize_asset_class(row.asset_class),
            exchange=row.exchange,
            status=row.status,
            tradable=row.tradable,
            fractionable=row.fractionable,
            shortable=row.shortable,
            easy_to_borrow=row.easy_to_borrow,
            marginable=row.marginable,
            attributes=[str(item) for item in raw_attributes],
            raw=raw_payload,
        )

    def list_assets(
        self,
        asset_class: AssetClass | str | None = None,
        query: str | None = None,
        exchange: str | None = None,
        tradable: bool | None = None,
        limit: int = 100,
    ) -> list[AssetMetadata]:
        self.ensure_fresh()
        resolved_asset_class = normalize_asset_class(asset_class)
        with SessionLocal() as session:
            asset_query = session.query(AssetCatalogEntry)
            if resolved_asset_class != AssetClass.UNKNOWN:
                asset_query = asset_query.filter(AssetCatalogEntry.asset_class == resolved_asset_class.value)
            if query:
                like = f"%{query.strip().upper()}%"
                asset_query = asset_query.filter(
                    or_(
                        func.upper(AssetCatalogEntry.symbol).like(like),
                        func.upper(AssetCatalogEntry.name).like(like),
                    )
                )
            if exchange:
                asset_query = asset_query.filter(AssetCatalogEntry.exchange == exchange)
            if tradable is not None:
                asset_query = asset_query.filter(AssetCatalogEntry.tradable == tradable)
            rows = (
                asset_query
                .order_by(AssetCatalogEntry.asset_class.asc(), AssetCatalogEntry.symbol.asc())
                .limit(limit)
                .all()
            )
        return [self._row_to_asset(row) for row in rows]

    def search_assets(self, query: str, limit: int = 25) -> list[AssetMetadata]:
        return self.list_assets(query=query, limit=limit)

    def get_asset(self, symbol: str) -> AssetMetadata | None:
        self.ensure_fresh()
        resolved_symbol = canonicalize_symbol(symbol)
        with SessionLocal() as session:
            row = (
                session.query(AssetCatalogEntry)
                .filter(AssetCatalogEntry.symbol == resolved_symbol)
                .one_or_none()
            )
        return self._row_to_asset(row) if row else None

    def get_stats(self) -> dict[str, int]:
        self.ensure_fresh()
        with SessionLocal() as session:
            rows = session.query(AssetCatalogEntry.asset_class, func.count(AssetCatalogEntry.id)).group_by(
                AssetCatalogEntry.asset_class
            )
            counts = {asset_class: count for asset_class, count in rows}
            tradable_count = session.query(func.count(AssetCatalogEntry.id)).filter(AssetCatalogEntry.tradable.is_(True)).scalar() or 0
        return {
            "total_assets": sum(counts.values()),
            "tradable_assets": tradable_count,
            "equities": counts.get(AssetClass.EQUITY.value, 0),
            "etfs": counts.get(AssetClass.ETF.value, 0),
            "crypto": counts.get(AssetClass.CRYPTO.value, 0),
            "options": counts.get(AssetClass.OPTION.value, 0),
        }

    def get_scan_universe(self, asset_class: AssetClass | str | None = None) -> list[AssetMetadata]:
        resolved_asset_class = normalize_asset_class(asset_class)
        allowed_classes = self.settings.enabled_asset_class_set
        if resolved_asset_class != AssetClass.UNKNOWN:
            allowed_classes = allowed_classes.intersection({resolved_asset_class})
            if not allowed_classes:
                return []

        explicit_symbol_order = self.settings.scan_symbol_allowlist
        include_symbols = {symbol.upper() for symbol in explicit_symbol_order}
        exclude_symbols = {symbol.upper() for symbol in self.settings.excluded_symbols}
        watchlist_symbols = {symbol.upper() for symbol in self.settings.watchlist_symbols} if not include_symbols else set()

        # Major symbols mode
        major_symbols = set()
        if self.settings.scan_universe_mode.lower() == "major" and not include_symbols:
            for symbol in self.settings.major_equity_symbols + self.settings.major_crypto_symbols:
                major_symbols.add(symbol.upper())

        assets = self.list_assets(limit=20_000, tradable=True)
        filtered: list[AssetMetadata] = []
        for asset in assets:
            symbol = asset.symbol.upper()
            if asset.asset_class not in allowed_classes:
                continue
            if symbol in exclude_symbols:
                continue
            if include_symbols and symbol not in include_symbols:
                continue
            if watchlist_symbols and symbol not in watchlist_symbols:
                continue
            if major_symbols and symbol not in major_symbols:
                continue
            if not asset.tradable or asset.status.lower() != "active":
                continue
            filtered.append(asset)

        if include_symbols:
            symbol_rank = {symbol: index for index, symbol in enumerate(explicit_symbol_order)}
            filtered.sort(key=lambda asset: symbol_rank.get(asset.symbol.upper(), len(symbol_rank)))
        return filtered
