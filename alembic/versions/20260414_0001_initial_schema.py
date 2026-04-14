"""Initial schema.

Revision ID: 20260414_0001
Revises:
Create Date: 2026-04-14 00:01:00
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260414_0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _side_enum(create_type: bool) -> sa.Enum:
    return sa.Enum("BUY", "SELL", name="side", create_type=create_type)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        _side_enum(create_type=True).create(bind, checkfirst=True)

    side_enum = _side_enum(create_type=False)

    op.create_table(
        "asset_catalog_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tradable", sa.Boolean(), nullable=False),
        sa.Column("fractionable", sa.Boolean(), nullable=False),
        sa.Column("shortable", sa.Boolean(), nullable=False),
        sa.Column("easy_to_borrow", sa.Boolean(), nullable=False),
        sa.Column("marginable", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_asset_catalog_entries_asset_class"), "asset_catalog_entries", ["asset_class"], unique=False)
    op.create_index(op.f("ix_asset_catalog_entries_exchange"), "asset_catalog_entries", ["exchange"], unique=False)
    op.create_index(op.f("ix_asset_catalog_entries_id"), "asset_catalog_entries", ["id"], unique=False)
    op.create_index(op.f("ix_asset_catalog_entries_symbol"), "asset_catalog_entries", ["symbol"], unique=True)
    op.create_index(op.f("ix_asset_catalog_entries_synced_at"), "asset_catalog_entries", ["synced_at"], unique=False)

    op.create_table(
        "asset_catalog_sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_asset_catalog_sync_runs_id"), "asset_catalog_sync_runs", ["id"], unique=False)

    op.create_table(
        "auto_trader_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("symbols_scanned", sa.Text(), nullable=True),
        sa.Column("signals_generated", sa.Text(), nullable=True),
        sa.Column("orders_submitted", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_auto_trader_runs_id"), "auto_trader_runs", ["id"], unique=False)

    op.create_table(
        "bot_run_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("run_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_run_history_id"), "bot_run_history", ["id"], unique=False)

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_equity_snapshots_id"), "equity_snapshots", ["id"], unique=False)

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("filled_at", sa.DateTime(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_fills_asset_class"), "fills", ["asset_class"], unique=False)
    op.create_index(op.f("ix_fills_filled_at"), "fills", ["filled_at"], unique=False)
    op.create_index(op.f("ix_fills_id"), "fills", ["id"], unique=False)
    op.create_index(op.f("ix_fills_order_id"), "fills", ["order_id"], unique=False)
    op.create_index(op.f("ix_fills_symbol"), "fills", ["symbol"], unique=False)

    op.create_table(
        "normalized_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("signal", sa.String(length=16), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_price", sa.Float(), nullable=True),
        sa.Column("position_size", sa.Float(), nullable=True),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("momentum_score", sa.Float(), nullable=True),
        sa.Column("liquidity_score", sa.Float(), nullable=True),
        sa.Column("spread_score", sa.Float(), nullable=True),
        sa.Column("regime_state", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_normalized_signals_asset_class"), "normalized_signals", ["asset_class"], unique=False)
    op.create_index(op.f("ix_normalized_signals_confidence_score"), "normalized_signals", ["confidence_score"], unique=False)
    op.create_index(op.f("ix_normalized_signals_generated_at"), "normalized_signals", ["generated_at"], unique=False)
    op.create_index(op.f("ix_normalized_signals_id"), "normalized_signals", ["id"], unique=False)
    op.create_index(op.f("ix_normalized_signals_strategy_name"), "normalized_signals", ["strategy_name"], unique=False)
    op.create_index(op.f("ix_normalized_signals_symbol"), "normalized_signals", ["symbol"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", side_enum, nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("is_dry_run", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_orders_id"), "orders", ["id"], unique=False)

    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("market_value", sa.Float(), nullable=True),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_position_snapshots_asset_class"), "position_snapshots", ["asset_class"], unique=False)
    op.create_index(op.f("ix_position_snapshots_id"), "position_snapshots", ["id"], unique=False)
    op.create_index(op.f("ix_position_snapshots_snapshot_at"), "position_snapshots", ["snapshot_at"], unique=False)
    op.create_index(op.f("ix_position_snapshots_symbol"), "position_snapshots", ["symbol"], unique=False)

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("side", side_enum, nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_positions_id"), "positions", ["id"], unique=False)

    op.create_table(
        "ranked_opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("price_change_pct", sa.Float(), nullable=True),
        sa.Column("momentum_score", sa.Float(), nullable=True),
        sa.Column("volatility_score", sa.Float(), nullable=True),
        sa.Column("liquidity_score", sa.Float(), nullable=True),
        sa.Column("spread_score", sa.Float(), nullable=True),
        sa.Column("tradability_score", sa.Float(), nullable=True),
        sa.Column("signal_quality_score", sa.Float(), nullable=True),
        sa.Column("regime_state", sa.String(length=32), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ranked_opportunities_asset_class"), "ranked_opportunities", ["asset_class"], unique=False)
    op.create_index(op.f("ix_ranked_opportunities_generated_at"), "ranked_opportunities", ["generated_at"], unique=False)
    op.create_index(op.f("ix_ranked_opportunities_id"), "ranked_opportunities", ["id"], unique=False)
    op.create_index(op.f("ix_ranked_opportunities_scanner_run_id"), "ranked_opportunities", ["scanner_run_id"], unique=False)
    op.create_index(op.f("ix_ranked_opportunities_signal_quality_score"), "ranked_opportunities", ["signal_quality_score"], unique=False)
    op.create_index(op.f("ix_ranked_opportunities_symbol"), "ranked_opportunities", ["symbol"], unique=False)

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_risk_events_id"), "risk_events", ["id"], unique=False)

    op.create_table(
        "scanner_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("asset_class", sa.String(length=32), nullable=True),
        sa.Column("symbols_scanned", sa.Integer(), nullable=False),
        sa.Column("signals_generated", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_scanner_runs_asset_class"), "scanner_runs", ["asset_class"], unique=False)
    op.create_index(op.f("ix_scanner_runs_id"), "scanner_runs", ["id"], unique=False)

    op.create_table(
        "signal_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("signal", sa.String(length=10), nullable=False),
        sa.Column("strength", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("trailing_stop", sa.Float(), nullable=True),
        sa.Column("momentum_score", sa.Float(), nullable=True),
        sa.Column("regime_state", sa.String(length=20), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_signal_events_id"), "signal_events", ["id"], unique=False)

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", side_enum, nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trades_id"), "trades", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trades_id"), table_name="trades")
    op.drop_table("trades")

    op.drop_index(op.f("ix_signal_events_id"), table_name="signal_events")
    op.drop_table("signal_events")

    op.drop_index(op.f("ix_scanner_runs_id"), table_name="scanner_runs")
    op.drop_index(op.f("ix_scanner_runs_asset_class"), table_name="scanner_runs")
    op.drop_table("scanner_runs")

    op.drop_index(op.f("ix_risk_events_id"), table_name="risk_events")
    op.drop_table("risk_events")

    op.drop_index(op.f("ix_ranked_opportunities_symbol"), table_name="ranked_opportunities")
    op.drop_index(op.f("ix_ranked_opportunities_signal_quality_score"), table_name="ranked_opportunities")
    op.drop_index(op.f("ix_ranked_opportunities_scanner_run_id"), table_name="ranked_opportunities")
    op.drop_index(op.f("ix_ranked_opportunities_id"), table_name="ranked_opportunities")
    op.drop_index(op.f("ix_ranked_opportunities_generated_at"), table_name="ranked_opportunities")
    op.drop_index(op.f("ix_ranked_opportunities_asset_class"), table_name="ranked_opportunities")
    op.drop_table("ranked_opportunities")

    op.drop_index(op.f("ix_positions_id"), table_name="positions")
    op.drop_table("positions")

    op.drop_index(op.f("ix_position_snapshots_symbol"), table_name="position_snapshots")
    op.drop_index(op.f("ix_position_snapshots_snapshot_at"), table_name="position_snapshots")
    op.drop_index(op.f("ix_position_snapshots_id"), table_name="position_snapshots")
    op.drop_index(op.f("ix_position_snapshots_asset_class"), table_name="position_snapshots")
    op.drop_table("position_snapshots")

    op.drop_index(op.f("ix_orders_id"), table_name="orders")
    op.drop_table("orders")

    op.drop_index(op.f("ix_normalized_signals_symbol"), table_name="normalized_signals")
    op.drop_index(op.f("ix_normalized_signals_strategy_name"), table_name="normalized_signals")
    op.drop_index(op.f("ix_normalized_signals_id"), table_name="normalized_signals")
    op.drop_index(op.f("ix_normalized_signals_generated_at"), table_name="normalized_signals")
    op.drop_index(op.f("ix_normalized_signals_confidence_score"), table_name="normalized_signals")
    op.drop_index(op.f("ix_normalized_signals_asset_class"), table_name="normalized_signals")
    op.drop_table("normalized_signals")

    op.drop_index(op.f("ix_fills_symbol"), table_name="fills")
    op.drop_index(op.f("ix_fills_order_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_id"), table_name="fills")
    op.drop_index(op.f("ix_fills_filled_at"), table_name="fills")
    op.drop_index(op.f("ix_fills_asset_class"), table_name="fills")
    op.drop_table("fills")

    op.drop_index(op.f("ix_equity_snapshots_id"), table_name="equity_snapshots")
    op.drop_table("equity_snapshots")

    op.drop_index(op.f("ix_bot_run_history_id"), table_name="bot_run_history")
    op.drop_table("bot_run_history")

    op.drop_index(op.f("ix_auto_trader_runs_id"), table_name="auto_trader_runs")
    op.drop_table("auto_trader_runs")

    op.drop_index(op.f("ix_asset_catalog_sync_runs_id"), table_name="asset_catalog_sync_runs")
    op.drop_table("asset_catalog_sync_runs")

    op.drop_index(op.f("ix_asset_catalog_entries_synced_at"), table_name="asset_catalog_entries")
    op.drop_index(op.f("ix_asset_catalog_entries_symbol"), table_name="asset_catalog_entries")
    op.drop_index(op.f("ix_asset_catalog_entries_id"), table_name="asset_catalog_entries")
    op.drop_index(op.f("ix_asset_catalog_entries_exchange"), table_name="asset_catalog_entries")
    op.drop_index(op.f("ix_asset_catalog_entries_asset_class"), table_name="asset_catalog_entries")
    op.drop_table("asset_catalog_entries")

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        _side_enum(create_type=True).drop(bind, checkfirst=True)
