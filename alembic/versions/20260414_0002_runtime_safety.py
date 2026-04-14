"""Add persistent runtime safety state.

Revision ID: 20260414_0002
Revises: 20260414_0001
Create Date: 2026-04-14 19:10:00
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260414_0002"
down_revision: str | None = "20260414_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_safety_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("halted", sa.Boolean(), nullable=False),
        sa.Column("halt_reason", sa.String(length=128), nullable=True),
        sa.Column("halt_rule", sa.String(length=128), nullable=True),
        sa.Column("halted_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_losing_exits", sa.Integer(), nullable=False),
        sa.Column("last_reconcile_status", sa.String(length=32), nullable=True),
        sa.Column("last_reconcile_summary_json", sa.Text(), nullable=True),
        sa.Column("lock_metadata_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_runtime_safety_state_id"), "runtime_safety_state", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_runtime_safety_state_id"), table_name="runtime_safety_state")
    op.drop_table("runtime_safety_state")
