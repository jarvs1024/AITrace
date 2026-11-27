# src/aitrace/notify/enqueue.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.notify.cards import detect_high_risk
from aitrace.persistence.models import SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask


def enqueue_batch_notify(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    event: dict[str, Any],
    batch_id: str,
    product_id: str,
    queue_key: str,
    now: datetime,
) -> None:
    """批次级通知任务。case_id='' 时 uq_task_batch(batch_id) 要求 batch_id 唯一，
    故 batch_id 列落 task_id，真实 batch_id 走 payload 与 queue_key。"""
    task_id = ids.new("T")
    queue.enqueue(
        NewTask(
            task_id=task_id,
            batch_id=task_id,
            session_id=None,
            round_id="",
            round_no=0,
            product_id=product_id,
            case_id="",
            task_type=TaskType.NOTIFY,
            queue_key=queue_key,
            payload={"batch_id": batch_id, "product_id": product_id, **event},
        ),
        now,
    )


def enqueue_case_notify(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    session_row: SessionModel,
    round_id: str,
    round_no: int,
    category: str,
    detail: str,
    confidence: float | None,
    now: datetime,
) -> None:
    """案例完成回写后的通知：首例/聚合卡，高危关键词命中时附加独立高危卡（嵌套 high_risk）。
    uq_task_case(session_id, round_no) 保证每案例轮次至多一条 notify 任务。"""
    payload: dict[str, Any] = {
        "event": "batch_card",
        "batch_id": session_row.batch_id,
        "session_id": session_row.session_id,
        "round_id": round_id,
        "round_no": round_no,
        "case_id": session_row.case_id,
        "category": category,
        "detail": (detail or "")[:200],
        "confidence": confidence,
        "product_id": session_row.product_id,
    }
    keywords = detect_high_risk(detail or "")
    if keywords:
        payload["high_risk"] = {"keywords": keywords, "detail": (detail or "")[:2000]}
    task_id = ids.new("T")
    queue.enqueue(
        NewTask(
            task_id=task_id,
            batch_id=session_row.batch_id if session_row.case_id else task_id,
            session_id=session_row.session_id,
            round_id=round_id,
            round_no=round_no,
            product_id=session_row.product_id,
            case_id=session_row.case_id,
            task_type=TaskType.NOTIFY,
            queue_key=session_row.batch_id,
            payload=payload,
        ),
        now,
    )


def pending_negated_exists(
    session: Session, batch_id: str, citing_session_id: str, citing_round_no: int
) -> bool:
    rows = (
        session.execute(
            select(TaskQueue).where(
                TaskQueue.task_type == TaskType.NOTIFY,
                TaskQueue.queue_state == QueueState.PENDING,
                TaskQueue.queue_key == batch_id,
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        payload = row.payload or {}
        if (
            payload.get("event") == "negated"
            and payload.get("citing_session_id") == citing_session_id
            and payload.get("citing_round_no") == citing_round_no
        ):
            return True
    return False
