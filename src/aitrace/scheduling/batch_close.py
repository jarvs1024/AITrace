# src/aitrace/scheduling/batch_close.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.notify.enqueue import enqueue_batch_notify
from aitrace.persistence.models import Batch, Round, SessionModel
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.router import create_batch_common

_TERMINAL = {"COMPLETED", "AWAITING_FEEDBACK", "VALID", "INVALID", "FAILED", "ABANDONED"}


def close_batch_if_done(session: Session, queue: QueueRepository, ids: Any, now: datetime) -> int:
    running = (
        session.execute(
            select(Batch).where(Batch.batch_state == "RUNNING").with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    closed = 0
    for batch in running:
        rounds = (
            session.execute(
                select(Round)
                .join(SessionModel, Round.session_id == SessionModel.session_id)
                .where(SessionModel.batch_id == batch.batch_id)
            )
            .scalars()
            .all()
        )
        if not rounds or any(r.round_status not in _TERMINAL for r in rounds):
            continue
        failed = [r for r in rounds if r.round_status in {"FAILED", "ABANDONED"}]
        gate = dict(batch.gate or {})
        if len(failed) >= 2 and not gate.get("batch_common_task_id"):
            create_batch_common(session, queue, ids, batch=batch, now=now)
        batch.batch_state = "COMPLETED" if not failed else "FAILED"
        batch.updated_at = now
        enqueue_batch_notify(
            session,
            queue,
            ids,
            event={"event": "final"},
            batch_id=batch.batch_id,
            product_id=batch.product_id,
            queue_key=batch.batch_id,
            now=now,
        )
        closed += 1
    session.flush()
    return closed
