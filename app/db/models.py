from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, Integer, String, Text
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(Enum(Side), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False)
    side = Column(Enum(Side), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
    status = Column(String(50), default="PENDING")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    is_dry_run = Column(Boolean, default=True)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False)
    quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    side = Column(Enum(Side), nullable=False)
    opened_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, nullable=False)
    realized_pnl = Column(Float, nullable=False)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    reason = Column(String(255), nullable=False)
    symbol = Column(String(20), nullable=True)
    details = Column(Text, nullable=True)
    is_blocked = Column(Boolean, default=True)


class AutoTraderRun(Base):
    __tablename__ = "auto_trader_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    symbols_scanned = Column(Text, nullable=True)  # JSON list
    signals_generated = Column(Text, nullable=True)  # JSON dict
    orders_submitted = Column(Text, nullable=True)  # JSON list
    error_message = Column(Text, nullable=True)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False)
    signal = Column(String(10), nullable=False)  # BUY, SELL, HOLD
    strength = Column(Float, nullable=True)
    price = Column(Float, nullable=False)
    reason = Column(Text, nullable=True)
    atr = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    trailing_stop = Column(Float, nullable=True)
    momentum_score = Column(Float, nullable=True)
    regime_state = Column(String(20), nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow)


class AssetCatalogEntry(Base):
    __tablename__ = "asset_catalog_entries"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    asset_class = Column(String(32), nullable=False, index=True)
    exchange = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="active")
    tradable = Column(Boolean, default=True, nullable=False)
    fractionable = Column(Boolean, default=False, nullable=False)
    shortable = Column(Boolean, default=False, nullable=False)
    easy_to_borrow = Column(Boolean, default=False, nullable=False)
    marginable = Column(Boolean, default=False, nullable=False)
    attributes = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="broker")
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    raw_payload = Column(Text, nullable=True)


class AssetCatalogSyncRun(Base):
    __tablename__ = "asset_catalog_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    source = Column(String(64), nullable=False, default="broker")
    asset_count = Column(Integer, nullable=False, default=0)
    cache_hit = Column(Boolean, default=False, nullable=False)
    status = Column(String(32), nullable=False, default="success")
    error_message = Column(Text, nullable=True)


class ScannerRun(Base):
    __tablename__ = "scanner_runs"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    asset_class = Column(String(32), nullable=True, index=True)
    symbols_scanned = Column(Integer, nullable=False, default=0)
    signals_generated = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="success")
    error_message = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)


class RankedOpportunityRecord(Base):
    __tablename__ = "ranked_opportunities"

    id = Column(Integer, primary_key=True, index=True)
    scanner_run_id = Column(Integer, nullable=True, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    asset_class = Column(String(32), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    last_price = Column(Float, nullable=True)
    price_change_pct = Column(Float, nullable=True)
    momentum_score = Column(Float, nullable=True)
    volatility_score = Column(Float, nullable=True)
    liquidity_score = Column(Float, nullable=True)
    spread_score = Column(Float, nullable=True)
    tradability_score = Column(Float, nullable=True)
    signal_quality_score = Column(Float, nullable=True, index=True)
    regime_state = Column(String(32), nullable=True)
    tags = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    metrics_json = Column(Text, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class NormalizedSignalRecord(Base):
    __tablename__ = "normalized_signals"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    asset_class = Column(String(32), nullable=False, index=True)
    strategy_name = Column(String(128), nullable=False, index=True)
    signal_type = Column(String(32), nullable=False, default="entry")
    direction = Column(String(32), nullable=False)
    signal = Column(String(16), nullable=False)
    confidence_score = Column(Float, nullable=True, index=True)
    entry_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    position_size = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)
    momentum_score = Column(Float, nullable=True)
    liquidity_score = Column(Float, nullable=True)
    spread_score = Column(Float, nullable=True)
    regime_state = Column(String(32), nullable=True)
    reason = Column(Text, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    metrics_json = Column(Text, nullable=True)


class FillRecord(Base):
    __tablename__ = "fills"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(128), nullable=True, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    asset_class = Column(String(32), nullable=False, index=True)
    side = Column(String(16), nullable=False)
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    status = Column(String(32), nullable=False, default="FILLED")
    filled_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    raw_payload = Column(Text, nullable=True)


class PositionSnapshotRecord(Base):
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    asset_class = Column(String(32), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    market_value = Column(Float, nullable=True)
    side = Column(String(16), nullable=False)
    exchange = Column(String(64), nullable=True)
    snapshot_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class BotRunHistory(Base):
    __tablename__ = "bot_run_history"

    id = Column(Integer, primary_key=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    run_type = Column(String(64), nullable=False, default="manual")
    status = Column(String(32), nullable=False, default="success")
    summary_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
