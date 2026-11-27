"""batch.gate JSONB 列（聚合闸预算与分组快照）

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batch", sa.Column("gate", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    )


def downgrade() -> None:
    op.drop_column("batch", "gate")
