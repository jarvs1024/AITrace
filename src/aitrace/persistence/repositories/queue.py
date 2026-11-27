from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.base import EPOCH
from aitrace.persistence.models import TaskQueue, TaskQueueRow, UserQueue
from aitrace.scheduling.slot_gate import acquire_slot, release_slot
from aitrace.scheduling.types import NewTask


class QueueRepository:
    def __init__(self, session: Session, *, lease_seconds: int = 600) -> None:
        self._session = session
        self._lease = timedelta(seconds=lease_seconds)

    def ensure_user_queue(self, queue_key: str, now: datetime) -> None:
        self._session.execute(
            insert(UserQueue)
            .values(
                queue_key=queue_key,
                state="ACTIVE",
                in_flight=0,
                paused_until=EPOCH,
                last_dispatch_at=None,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["queue_key"])
        )

    def enqueue(self, task: NewTask, now: datetime) -> TaskQueueRow:
        self.ensure_user_queue(task.queue_key, now)
        row = TaskQueue(
            task_id=task.task_id,
            batch_id=task.batch_id,
            session_id=task.session_id,
            round_id=task.round_id,
            round_no=task.round_no,
            product_id=task.product_id,
            case_id=task.case_id,
            task_type=str(task.task_type),
            queue_key=task.queue_key,
            queue_state=QueueState.PENDING,
            dispatch_phase=None,
            priority=task.priority,
            depends_on_task_id=None,
            not_before=None,
            attempts=0,
            max_attempts=task.max_attempts,
            lease_owner=None,
            lease_generation=0,
            lease_until=EPOCH,
            next_retry_at=None,
            callback_status="PENDING",
            payload=task.payload,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def claim_clean(self, owner: str, now: datetime) -> TaskQueueRow | None:
        row = self._session.execute(
            select(TaskQueue)
            .where(
                TaskQueue.queue_state == QueueState.PENDING,
                TaskQueue.task_type == TaskType.CASE_CLEAN,
                TaskQueue.lease_until < now,
                or_(TaskQueue.not_before.is_(None), TaskQueue.not_before <= now),
                or_(TaskQueue.next_retry_at.is_(None), TaskQueue.next_retry_at <= now),
            )
            .order_by(TaskQueue.priority.asc(), TaskQueue.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        row.lease_owner = owner
        row.lease_generation += 1
        row.lease_until = now + self._lease
        row.queue_state = QueueState.CLEANING
        row.updated_at = now
        self._session.flush()
        return row

    def claim_ai(self, owner: str, now: datetime) -> TaskQueueRow | None:
        row = self._session.execute(
            select(TaskQueue)
            .join(UserQueue, UserQueue.queue_key == TaskQueue.queue_key)
            .where(
                TaskQueue.queue_state == QueueState.READY,
                TaskQueue.task_type.in_([TaskType.CASE_AI, TaskType.BATCH_COMMON]),
                TaskQueue.lease_until < now,
                or_(TaskQueue.not_before.is_(None), TaskQueue.not_before <= now),
                or_(TaskQueue.next_retry_at.is_(None), TaskQueue.next_retry_at <= now),
                UserQueue.state == "ACTIVE",
                UserQueue.paused_until <= now,
                UserQueue.in_flight < 1,
            )
            .order_by(
                func.coalesce(UserQueue.last_dispatch_at, EPOCH).asc(),
                TaskQueue.priority.asc(),
                TaskQueue.created_at.asc(),
            )
            .with_for_update(skip_locked=True, of=TaskQueue)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        if not acquire_slot(self._session, row.queue_key, now):
            return None
        row.queue_state = QueueState.AI_RUNNING
        row.dispatch_phase = "JIT_FETCH"
        row.lease_owner = owner
        row.lease_generation += 1
        row.lease_until = now + self._lease
        row.updated_at = now
        self._session.flush()
        return row

    def finish(
        self,
        task_id: str,
        owner: str,
        generation: int,
        state: QueueState,
        now: datetime,
        error: dict[str, object] | None = None,
    ) -> bool:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.lease_owner != owner or row.lease_generation != generation:
            return False
        was_ai = row.queue_state == QueueState.AI_RUNNING
        row.queue_state = state
        row.dispatch_phase = None
        row.lease_owner = None
        row.lease_until = EPOCH
        row.error = error
        row.updated_at = now
        if was_ai:
            self._release_slot(row.queue_key, now)
        self._session.flush()
        return True

    def retry(
        self,
        task_id: str,
        owner: str,
        generation: int,
        now: datetime,
        error: dict[str, object],
    ) -> QueueState:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one()
        if row.lease_owner != owner or row.lease_generation != generation:
            raise ValueError("租约已被回收，重试写入被 fencing 拒绝")
        was_ai = row.queue_state == QueueState.AI_RUNNING
        row.attempts += 1
        row.error = error
        row.lease_owner = None
        row.lease_until = EPOCH
        row.dispatch_phase = None
        row.updated_at = now
        if was_ai:
            self._release_slot(row.queue_key, now)
        if row.attempts >= row.max_attempts:
            row.queue_state = QueueState.FAILED
        else:
            row.queue_state = (
                QueueState.PENDING if row.task_type == TaskType.CASE_CLEAN else QueueState.READY
            )
            delay = min(30 * (2 ** (row.attempts - 1)), 1800)
            row.next_retry_at = now + timedelta(seconds=delay)
        self._session.flush()
        return QueueState(row.queue_state)

    def reclaim_confirmed(self, task_id: str, owner: str, generation: int, now: datetime) -> bool:
        row = self._session.execute(
            select(TaskQueue)
            .where(TaskQueue.task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            row is None
            or row.lease_owner != owner
            or row.lease_generation != generation
            or row.queue_state != QueueState.AI_RUNNING
            or row.lease_until >= now
        ):
            return False
        row.queue_state = QueueState.READY
        row.dispatch_phase = None
        row.lease_owner = None
        row.lease_until = EPOCH
        row.updated_at = now
        self._release_slot(row.queue_key, now)
        self._session.flush()
        return True

    def _release_slot(self, queue_key: str, now: datetime) -> None:
        release_slot(self._session, queue_key, now)

    def claim_callback(self, owner: str, now: datetime) -> TaskQueueRow | None:
        row = self._session.execute(
            select(TaskQueue)
            .where(
                TaskQueue.queue_state == QueueState.PENDING,
                TaskQueue.task_type == TaskType.CALLBACK,
                TaskQueue.lease_until < now,
                or_(TaskQueue.next_retry_at.is_(None), TaskQueue.next_retry_at <= now),
            )
            .order_by(TaskQueue.priority.asc(), TaskQueue.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        row.lease_owner = owner
        row.lease_generation += 1
        row.lease_until = now + self._lease
        row.updated_at = now
        self._session.flush()
        return row

    def retry_callback(
        self, task_id: str, owner: str, generation: int, now: datetime, error: dict[str, object]
    ) -> QueueState:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one()
        if row.lease_owner != owner or row.lease_generation != generation:
            raise ValueError("租约已被回收，重试写入被 fencing 拒绝")
        row.attempts += 1
        row.error = error
        row.lease_owner = None
        row.lease_until = EPOCH
        row.updated_at = now
        if row.attempts >= row.max_attempts:
            row.queue_state = QueueState.FAILED
            row.callback_status = "FAILED"
        else:
            row.queue_state = QueueState.PENDING
            delay = min(30 * (2 ** (row.attempts - 1)), 1800)
            row.next_retry_at = now + timedelta(seconds=delay)
        self._session.flush()
        return QueueState(row.queue_state)
