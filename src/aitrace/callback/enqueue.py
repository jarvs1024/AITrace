# src/aitrace/callback/enqueue.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aitrace.callback.sequencer import next_batch_seq, next_session_seq
from aitrace.core.enums import Stage, TaskType
from aitrace.persistence.models import TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

_CALLBACK_PRIORITY_PROGRESS = 20
_CALLBACK_PRIORITY_RESULT = 10


def enqueue_progress(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    task: TaskQueue,
    stage: Stage,
    progress: int,
    waiting_reason: str | None,
    message: str,
    now: datetime,
) -> None:
    # PROGRESS/RESULT 仅由分析任务路径触发（会话归属由路由保证非空）
    assert task.session_id is not None
    payload = {
        "event": "PROGRESS",
        "session_id": task.session_id,
        "batch_id": task.batch_id,
        "case_id": task.case_id,
        "round_no": task.round_no,
        "seq": next_session_seq(session, task.session_id, task.round_no),
        "batch_seq": next_batch_seq(session, task.batch_id),
        "stage": str(stage),
        "progress": progress,
        "waiting_reason": waiting_reason,
        "message": message,
        "report": None,
        "citation": None,
        "queue_event": None,
        "sessions": None,
    }
    _enqueue(session, queue, ids, task, payload, _CALLBACK_PRIORITY_PROGRESS, now)


def enqueue_result(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    task: TaskQueue,
    report_payload: dict[str, Any],
    citation: dict[str, Any] | None,
    now: datetime,
) -> None:
    # PROGRESS/RESULT 仅由分析任务路径触发（会话归属由路由保证非空）
    assert task.session_id is not None
    payload = {
        "event": "RESULT",
        "session_id": task.session_id,
        "batch_id": task.batch_id,
        "case_id": task.case_id,
        "round_no": task.round_no,
        "seq": next_session_seq(session, task.session_id, task.round_no),
        "batch_seq": next_batch_seq(session, task.batch_id),
        "stage": "COMPLETED",
        "progress": 100,
        "waiting_reason": None,
        "message": "知因分析完成（点击查看根因报告）",
        "report": report_payload,
        "citation": citation,
        "queue_event": None,
        "sessions": None,
    }
    _enqueue(session, queue, ids, task, payload, _CALLBACK_PRIORITY_RESULT, now)


def enqueue_queue_event(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    queue_key: str,
    event_type: str,
    reason: str,
    affected_tasks: list[str],
    count: int,
    paused_until: datetime | None,
    now: datetime,
) -> None:
    queue_event = {
        "type": event_type,
        "reason": reason,
        "affected_tasks": affected_tasks,
        "count": count,
        "paused_until": paused_until.isoformat() if paused_until else None,
    }
    payload = {
        "event": "QUEUE_STATE",
        "session_id": "",
        "batch_id": "",
        "case_id": "",
        "round_no": 0,
        "seq": 0,
        "batch_seq": 0,
        "stage": "QUEUED",
        "progress": None,
        "waiting_reason": "paused_auth",
        "message": f"令牌队列状态变更：{event_type}（{reason}）",
        "report": None,
        "citation": None,
        "queue_event": queue_event,
        "sessions": None,
    }
    task = TaskQueue(
        task_id=ids.new("T"),
        batch_id="",
        session_id=None,
        round_id="",
        round_no=0,
        product_id="",
        case_id="",
        task_type=TaskType.CALLBACK,
        queue_key=queue_key,
        queue_state="PENDING",
        priority=_CALLBACK_PRIORITY_RESULT,
        max_attempts=5,
        payload=payload,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()


def _enqueue(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    task: TaskQueue,
    payload: dict[str, Any],
    priority: int,
    now: datetime,
) -> None:
    queue.enqueue(
        NewTask(
            task_id=ids.new("T"),
            batch_id=task.batch_id,
            session_id=task.session_id,
            round_id=task.round_id,
            round_no=task.round_no,
            product_id=task.product_id,
            case_id=task.case_id,
            task_type=TaskType.CALLBACK,
            queue_key=task.queue_key,
            priority=priority,
            payload=payload,
        ),
        now,
    )
