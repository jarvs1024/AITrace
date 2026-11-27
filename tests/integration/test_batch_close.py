# tests/integration/test_batch_close.py
from datetime import UTC, datetime

from sqlalchemy import select

from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.persistence.models import Batch, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.batch_close import close_batch_if_done

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed(db_session, *, statuses: list[str], batch_state: str = "RUNNING") -> None:
    db_session.add(
        Batch(
            batch_id="B1",
            product_id="ssd_9560",
            trigger_user_id="zhangsan",
            batch_task_id="T-BATCH-B1",
            failed_cases=[{"case_id": f"C{i}"} for i in range(len(statuses))],
            passed_cases=[],
            gate={},
            batch_state=batch_state,
            updated_at=_NOW,
        )
    )
    for i, status in enumerate(statuses):
        sid = f"s{i}"
        db_session.add(
            SessionModel(
                session_id=sid,
                product_id="ssd_9560",
                batch_id="B1",
                case_id=f"C{i}",
                domain_id="enterprise",
                profile_version="v1",
                trigger_user_id="zhangsan",
                hardware_type="SSD",
                stable_fingerprint=f"fp-{i}",
                env_topology={},
                git_context={},
                created_at=_NOW,
            )
        )
        db_session.flush()
        db_session.add(
            Round(
                round_id=f"r{i}",
                session_id=sid,
                round_no=1,
                trigger_user_id="zhangsan",
                queue_key="zhangsan",
                stage=Stage.COMPLETED.value,
                round_status=status,
                started_at=_NOW,
                finished_at=_NOW if status != "RUNNING" else None,
            )
        )
    db_session.flush()


def test_close_all_completed_batch(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, statuses=["AWAITING_FEEDBACK", "AWAITING_FEEDBACK"])
    closed = close_batch_if_done(db_session, queue, _Ids(), _NOW)
    assert closed == 1
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.batch_state == "COMPLETED"


def test_close_batch_with_failures(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, statuses=["AWAITING_FEEDBACK", "FAILED"])
    closed = close_batch_if_done(db_session, queue, _Ids(), _NOW)
    assert closed == 1
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.batch_state == "FAILED"


def test_running_batch_stays_open(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, statuses=["AWAITING_FEEDBACK", "RUNNING"])
    closed = close_batch_if_done(db_session, queue, _Ids(), _NOW)
    assert closed == 0
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.batch_state == "RUNNING"


def test_two_failures_triggers_batch_common_before_close(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed(db_session, statuses=["FAILED", "FAILED"])
    closed = close_batch_if_done(db_session, queue, _Ids(), _NOW)
    assert closed == 1
    batch = db_session.execute(select(Batch)).scalar_one()
    assert batch.batch_state == "FAILED"
    assert batch.gate.get("batch_common_task_id")
    common = db_session.execute(
        select(TaskQueue).where(TaskQueue.task_type == TaskType.BATCH_COMMON)
    ).scalar_one()
    assert common.queue_state == QueueState.READY.value
    assert common.case_id == ""
