"""task_queue 部分唯一索引排除 CALLBACK 行（R-M2-11E）

M0 冻结的 uq_task_case（case_id<>''）与 uq_task_batch（case_id=''）在设计时
task_queue 只承载分析任务。Task 7（spec §6.5.2）起回调行同表存储：同一用例轮次
的 PROGRESS 与 RESULT、连续多条 QUEUE_STATE（batch_id=''）都会撞唯一键，
导致进度事件物理上无法送达。排除 CALLBACK 后仍保留「每用例轮次一条分析任务」
与「每批一条批级任务」的不变量。

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_task_case", table_name="task_queue", postgresql_where=sa.text("case_id <> ''")
    )
    op.create_index(
        "uq_task_case",
        "task_queue",
        ["session_id", "round_no", "task_type"],
        unique=True,
        postgresql_where=sa.text("case_id <> '' AND task_type <> 'callback'"),
    )
    op.drop_index(
        "uq_task_batch", table_name="task_queue", postgresql_where=sa.text("case_id = ''")
    )
    op.create_index(
        "uq_task_batch",
        "task_queue",
        ["batch_id", "task_type"],
        unique=True,
        postgresql_where=sa.text("case_id = '' AND task_type <> 'callback'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_task_batch",
        table_name="task_queue",
        postgresql_where=sa.text("case_id = '' AND task_type <> 'callback'"),
    )
    op.create_index(
        "uq_task_batch",
        "task_queue",
        ["batch_id", "task_type"],
        unique=True,
        postgresql_where=sa.text("case_id = ''"),
    )
    op.drop_index(
        "uq_task_case",
        table_name="task_queue",
        postgresql_where=sa.text("case_id <> '' AND task_type <> 'callback'"),
    )
    op.create_index(
        "uq_task_case",
        "task_queue",
        ["session_id", "round_no", "task_type"],
        unique=True,
        postgresql_where=sa.text("case_id <> ''"),
    )
