# src/aitrace/scheduling/pause.py
"""队列 PAUSED_AUTH 生命周期：暂停（指数退避）/ 恢复 / 放弃。

`user_queue.last_error` 是 M0 冻结的 String(255) 列（迁移 0001），按 spec §7.4
只存错误码 / 脱敏摘要：写入 JSON 文本（message 截断至 200 字符），读取时反序列化，
对 None 或遗留非 JSON 值回退空 dict（R-M2-10A）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.callback.enqueue import enqueue_queue_event
from aitrace.core.enums import QueueState, TaskType
from aitrace.core.settings import AISettings
from aitrace.persistence.models import TaskQueue, UserQueue
from aitrace.persistence.repositories.queue import QueueRepository

_MAX_MESSAGE_CHARS = 200


def _load_last_error(row: UserQueue) -> dict[str, Any]:
    raw = row.last_error
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump_last_error(error: dict[str, Any]) -> str:
    return json.dumps(error, ensure_ascii=False)


def pause_queue(
    session: Session,
    queue: QueueRepository,
    ids: object,
    *,
    queue_key: str,
    now: datetime,
    code: str,
    message: str,
    settings: AISettings | None = None,
) -> None:
    if settings is None:
        settings = AISettings()
    row = session.execute(
        select(UserQueue).where(UserQueue.queue_key == queue_key).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        queue.ensure_user_queue(queue_key, now)
        row = session.execute(
            select(UserQueue).where(UserQueue.queue_key == queue_key).with_for_update()
        ).scalar_one()
    was_paused = row.state == "PAUSED_AUTH"
    last = _load_last_error(row)
    pause_count = int(last.get("pause_count", 0)) + 1
    backoff = min(
        settings.pause_backoff_base_seconds * (2 ** (pause_count - 1)),
        settings.pause_backoff_max_seconds,
    )
    row.state = "PAUSED_AUTH"
    row.paused_until = now + timedelta(seconds=backoff)
    row.last_error = _dump_last_error(
        {"code": code, "message": message[:_MAX_MESSAGE_CHARS], "pause_count": pause_count}
    )
    row.updated_at = now
    if not was_paused:
        enqueue_queue_event(
            session,
            queue,
            ids,
            queue_key=queue_key,
            event_type="PAUSED_AUTH",
            reason=code,
            affected_tasks=[],
            count=0,
            paused_until=row.paused_until,
            now=now,
        )
    session.flush()


def recover_paused(session: Session, queue: QueueRepository, ids: object, now: datetime) -> int:
    rows = (
        session.execute(
            select(UserQueue)
            .where(UserQueue.state == "PAUSED_AUTH", UserQueue.paused_until <= now)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    recovered = 0
    for row in rows:
        last_code = _load_last_error(row).get("code", "UNKNOWN")
        row.state = "ACTIVE"
        row.paused_until = datetime(1970, 1, 1, tzinfo=now.tzinfo)
        row.last_error = _dump_last_error({"code": last_code, "pause_count": 0})
        row.updated_at = now
        enqueue_queue_event(
            session,
            queue,
            ids,
            queue_key=row.queue_key,
            event_type="RESUMED",
            reason=last_code,
            affected_tasks=[],
            count=0,
            paused_until=None,
            now=now,
        )
        recovered += 1
    session.flush()
    return recovered


def abandon_stalled(
    session: Session,
    queue: QueueRepository,
    ids: object,
    now: datetime,
    *,
    cap_seconds: int,
) -> int:
    rows = (
        session.execute(
            select(UserQueue)
            .where(
                UserQueue.state == "PAUSED_AUTH",
                UserQueue.paused_until <= now - timedelta(seconds=cap_seconds),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    abandoned = 0
    for row in rows:
        tasks = (
            session.execute(
                select(TaskQueue)
                .where(
                    TaskQueue.queue_key == row.queue_key,
                    TaskQueue.queue_state == QueueState.READY.value,
                    TaskQueue.task_type.in_([TaskType.CASE_AI, TaskType.BATCH_COMMON]),
                )
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for task in tasks:
            task.queue_state = QueueState.FAILED.value
            task.error = {"code": "TOKEN_REQUIRED", "message": "令牌暂停超上限，请重新授权"}
            task.updated_at = now
            abandoned += 1
        row.state = "ACTIVE"
        row.paused_until = datetime(1970, 1, 1, tzinfo=now.tzinfo)
        row.updated_at = now
        enqueue_queue_event(
            session,
            queue,
            ids,
            queue_key=row.queue_key,
            event_type="ABANDONED",
            reason="PAUSE_TIMEOUT",
            affected_tasks=[t.task_id for t in tasks],
            count=len(tasks),
            paused_until=None,
            now=now,
        )
    session.flush()
    return abandoned
