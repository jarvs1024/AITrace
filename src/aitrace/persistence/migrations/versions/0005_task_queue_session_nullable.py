"""task_queue.session_id 放开 NOT NULL（R-M2-13B）

spec §6.5.2 队列级事件（QUEUE_STATE）回调行无会话归属，原 M0 冻结的
session_id NOT NULL 使 enqueue_queue_event 无法落库（FK 违规导致整个调度
tick 回滚、PAUSED_AUTH 状态丢失）。放开后仅队列事件回调行写 NULL；分析
任务行（CASE_AI/BATCH_COMMON/CASE_CLEAN）仍由路由保证非空。

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "task_queue",
        "session_id",
        existing_type=sa.String(40),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM task_queue WHERE session_id IS NULL")
    op.alter_column(
        "task_queue",
        "session_id",
        existing_type=sa.String(40),
        nullable=False,
    )
