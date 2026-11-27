from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _session(factory: sessionmaker[Session]) -> Session:
    return factory()


def _task(task_id: str = "T1") -> NewTask:
    return NewTask(
        task_id=task_id,
        batch_id="B1",
        session_id="S1",
        round_id="R1",
        round_no=1,
        product_id="ssd_9560",
        case_id="C1",
        task_type=TaskType.CASE_CLEAN,
        queue_key="zhangsan",
    )


@pytest.fixture
def factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def seeded(db_session: Session) -> Session:
    db_session.execute(
        text(
            "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
            "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
            "env_topology, git_context, created_at) VALUES "
            "('S1','ssd_9560','B1','C1','enterprise','v1','zhangsan','SSD',1,'RUNNING',"
            "'{}','{}',now())"
        )
    )
    db_session.commit()
    return db_session


def test_enqueue_creates_pending_task_and_active_queue(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    row = repo.enqueue(_task(), NOW)
    seeded.commit()
    assert row.queue_state == QueueState.PENDING
    assert row.lease_generation == 0
    state = seeded.execute(
        text("SELECT state, in_flight FROM user_queue WHERE queue_key='zhangsan'")
    ).one()
    assert state == ("ACTIVE", 0)


def test_clean_claim_uses_lease_and_fences_old_owner(
    seeded: Session, factory: sessionmaker[Session]
) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()

    claimed = repo.claim_clean("worker-a", NOW)
    seeded.commit()
    assert claimed is not None
    assert claimed.queue_state == QueueState.CLEANING
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_generation == 1

    stale = repo.finish("T1", "worker-b", 1, QueueState.DONE, NOW)
    assert stale is False

    assert repo.finish("T1", "worker-a", 1, QueueState.DONE, NOW) is True
    seeded.commit()
    assert (
        seeded.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one()
        == "DONE"
    )


def test_retry_returns_task_to_pending_with_backoff(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()
    repo.claim_clean("worker-a", NOW)
    seeded.commit()

    state = repo.retry("T1", "worker-a", 1, NOW, {"code": "IO_ERROR", "message": "读取失败"})
    seeded.commit()
    row = seeded.execute(
        text("SELECT queue_state, attempts, next_retry_at FROM task_queue WHERE task_id='T1'")
    ).one()
    assert state == QueueState.PENDING
    assert row.queue_state == "PENDING"
    assert row.attempts == 1
    assert row.next_retry_at > NOW


def test_retry_exhaustion_fails_task(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()
    repo.claim_clean("worker-a", NOW)
    seeded.commit()
    seeded.execute(text("UPDATE task_queue SET attempts=2, max_attempts=3 WHERE task_id='T1'"))
    seeded.commit()
    state = repo.retry("T1", "worker-a", 1, NOW, {"code": "IO_ERROR", "message": "读取失败"})
    seeded.commit()
    assert state == QueueState.FAILED
