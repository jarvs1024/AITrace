# tests/integration/test_router.py
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.settings import AISettings
from aitrace.persistence.models import Batch, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.router import route_ready_cleans

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed(db_session, *, n_sessions: int, fps: list[str], batch_id="B1") -> None:
    db_session.add(
        Batch(
            batch_id=batch_id,
            product_id="ssd_9560",
            trigger_user_id="zhangsan",
            batch_task_id=f"T-BATCH-{batch_id}",
            failed_cases=[{"case_id": f"C{i}"} for i in range(n_sessions)],
            passed_cases=[],
            gate={},
            updated_at=_NOW,
        )
    )
    for i in range(n_sessions):
        sid = f"s{i}"
        db_session.add(
            SessionModel(
                session_id=sid,
                product_id="ssd_9560",
                batch_id=batch_id,
                case_id=f"C{i}",
                domain_id="enterprise",
                profile_version="v1",
                trigger_user_id="zhangsan",
                hardware_type="SSD",
                stable_fingerprint=fps[i],
                env_topology={},
                git_context={},
                created_at=_NOW + timedelta(seconds=i),
            )
        )
        db_session.flush()
        db_session.add(
            Round(
                round_id=f"round-{sid}",
                session_id=sid,
                round_no=1,
                trigger_user_id="zhangsan",
                queue_key="zhangsan",
                stage=Stage.CLEANING.value,
                round_status="RUNNING",
                started_at=_NOW,
            )
        )
        db_session.add(
            TaskQueue(
                task_id=f"task-{sid}",
                batch_id=batch_id,
                session_id=sid,
                round_id=f"round-{sid}",
                round_no=1,
                product_id="ssd_9560",
                case_id=f"C{i}",
                task_type=TaskType.CASE_CLEAN,
                queue_key="zhangsan",
                queue_state=QueueState.READY.value,
                created_at=_NOW + timedelta(seconds=i),
                updated_at=_NOW,
            )
        )
    db_session.flush()


def _tasks(db_session) -> list[TaskQueue]:
    return db_session.execute(select(TaskQueue).order_by(TaskQueue.created_at)).scalars().all()


def test_leader_becomes_case_ai_and_followers_wait(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, n_sessions=3, fps=["fp-a", "fp-a", "fp-b"])
    routed = route_ready_cleans(db_session, queue, _Ids(), _NOW, AISettings())
    assert routed == 3
    tasks = _tasks(db_session)
    leader = next(t for t in tasks if t.session_id == "s0")
    follower = next(t for t in tasks if t.session_id == "s1")
    assert leader.task_type == TaskType.CASE_AI
    assert leader.queue_state == QueueState.READY.value
    assert follower.queue_state == QueueState.WAITING.value
    assert follower.depends_on_task_id == leader.task_id
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.gate["ai_used"] == 2  # fp-a 组 1 + fp-b 组 1


def test_budget_exhausted_new_group_gets_rule_other_report(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, n_sessions=4, fps=["fp-a", "fp-b", "fp-c", "fp-d"])
    settings = AISettings(batch_ai_budget=2)
    routed = route_ready_cleans(db_session, queue, _Ids(), _NOW, settings)
    assert routed == 4
    tasks = _tasks(db_session)
    done_rule = [t for t in tasks if t.queue_state == QueueState.DONE.value]
    assert len(done_rule) == 2  # fp-c / fp-d 预算耗尽 → 规则 OTHER
    for t in done_rule:
        report = (
            db_session.execute(select(Round).where(Round.round_id == t.round_id))
            .scalar_one()
            .report_id
        )
        assert report is not None
        assert t.task_type == TaskType.CASE_CLEAN


def test_escalation_group_waits_on_batch_common(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, n_sessions=6, fps=["fp-a"] * 5 + ["fp-b"])
    routed = route_ready_cleans(db_session, queue, _Ids(), _NOW, AISettings())
    assert routed == 6
    tasks = _tasks(db_session)
    common = next(t for t in tasks if t.task_type == TaskType.BATCH_COMMON)
    assert common.case_id == ""
    assert common.queue_state == QueueState.READY.value
    waiting = [t for t in tasks if t.session_id != "" and t.queue_state == QueueState.WAITING.value]
    assert len(waiting) == 5
    assert all(t.depends_on_task_id == common.task_id for t in waiting)
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.gate["batch_common_task_id"] == common.task_id
    assert batch.gate["escalated"] is True


def test_create_batch_common_idempotent(db_session) -> None:
    from aitrace.scheduling.router import create_batch_common

    queue = QueueRepository(db_session)
    _seed(db_session, n_sessions=1, fps=["fp-a"])
    batch = db_session.execute(select(Batch)).scalar_one()
    first = create_batch_common(db_session, queue, _Ids(), batch=batch, now=_NOW)
    second = create_batch_common(db_session, queue, _Ids(), batch=batch, now=_NOW)
    assert first == second
