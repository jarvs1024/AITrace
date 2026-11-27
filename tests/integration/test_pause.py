# tests/integration/test_pause.py
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.models import SessionModel, TaskQueue, UserQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.pause import abandon_stalled, pause_queue, recover_paused

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed(db_session, queue_key="zhangsan", *, state="ACTIVE", paused_until=None) -> None:
    # task_queue.session_id 有 FK → session；回调事件行 session_id="" 需要占位 session 行。
    db_session.add(
        SessionModel(
            session_id="",
            product_id="ssd_9560",
            batch_id="",
            case_id="",
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            latest_round_no=1,
            status="RUNNING",
            env_topology={},
            git_context={},
            created_at=_NOW,
        )
    )
    row = UserQueue(
        queue_key=queue_key,
        state=state,
        in_flight=0,
        paused_until=paused_until or datetime(1970, 1, 1, tzinfo=UTC),
        last_error=None,
        last_dispatch_at=None,
        updated_at=_NOW,
    )
    db_session.add(row)
    db_session.flush()


def _ready_ai_task(db_session, task_id="task-ai-1", queue_key="zhangsan") -> None:
    # task_queue.session_id 有 FK → session，先落 session 行。
    db_session.add(
        SessionModel(
            session_id="s1",
            product_id="ssd_9560",
            batch_id="B1",
            case_id="C1",
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            latest_round_no=1,
            status="RUNNING",
            env_topology={},
            git_context={},
            created_at=_NOW,
        )
    )
    db_session.flush()
    db_session.add(
        TaskQueue(
            task_id=task_id,
            batch_id="B1",
            session_id="s1",
            round_id="r1",
            round_no=1,
            product_id="ssd_9560",
            case_id="C1",
            task_type=TaskType.CASE_AI,
            queue_key=queue_key,
            queue_state=QueueState.READY.value,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    db_session.flush()


def test_pause_sets_paused_auth_and_backoff(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session)
    pause_queue(
        db_session,
        queue,
        _Ids(),
        queue_key="zhangsan",
        now=_NOW,
        code="TOKEN_EXPIRED",
        message="令牌已过期",
    )
    row = db_session.execute(select(UserQueue)).scalar_one()
    assert row.state == "PAUSED_AUTH"
    assert row.paused_until == _NOW + timedelta(seconds=60)
    assert json.loads(row.last_error)["pause_count"] == 1
    # 第二次暂停退避翻倍
    pause_queue(
        db_session,
        queue,
        _Ids(),
        queue_key="zhangsan",
        now=_NOW,
        code="TOKEN_EXPIRED",
        message="令牌已过期",
    )
    db_session.refresh(row)
    assert row.paused_until == _NOW + timedelta(seconds=120)
    assert json.loads(row.last_error)["pause_count"] == 2


def test_pause_event_emitted_only_on_transition(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session)
    pause_queue(
        db_session, queue, _Ids(), queue_key="zhangsan", now=_NOW, code="TOKEN_EXPIRED", message="x"
    )
    events = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload["queue_event"]["type"] == "PAUSED_AUTH"
    pause_queue(
        db_session, queue, _Ids(), queue_key="zhangsan", now=_NOW, code="TOKEN_EXPIRED", message="x"
    )
    assert (
        len(
            db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
            .scalars()
            .all()
        )
        == 1
    )


def test_recover_paused_returns_to_active(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, state="PAUSED_AUTH", paused_until=_NOW - timedelta(seconds=1))
    recovered = recover_paused(db_session, queue, _Ids(), _NOW)
    assert recovered == 1
    row = db_session.execute(select(UserQueue)).scalar_one()
    assert row.state == "ACTIVE"
    events = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    assert events[-1].payload["queue_event"]["type"] == "RESUMED"


def test_abandon_stalled_fails_ready_ai_tasks(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(
        db_session,
        state="PAUSED_AUTH",
        paused_until=_NOW - timedelta(hours=7),
    )
    _ready_ai_task(db_session)
    abandoned = abandon_stalled(db_session, queue, _Ids(), _NOW, cap_seconds=21600)
    assert abandoned == 1
    row = db_session.execute(select(UserQueue)).scalar_one()
    assert row.state == "ACTIVE"
    task = db_session.execute(
        select(TaskQueue).where(TaskQueue.task_id == "task-ai-1")
    ).scalar_one()
    assert task.queue_state == QueueState.FAILED.value
    assert task.error["code"] == "TOKEN_REQUIRED"
    events = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    assert events[-1].payload["queue_event"]["type"] == "ABANDONED"
    assert events[-1].payload["queue_event"]["reason"] == "PAUSE_TIMEOUT"
    assert events[-1].payload["queue_event"]["count"] == 1
