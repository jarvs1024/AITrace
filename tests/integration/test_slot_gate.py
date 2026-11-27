from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.slot_gate import acquire_slot, release_slot

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def test_slot_is_exclusive(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is True
    assert acquire_slot(db_session, "u1", NOW) is False
    db_session.commit()
    release_slot(db_session, "u1", NOW)
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is True


def test_paused_queue_rejects_slot(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    db_session.execute(
        text("UPDATE user_queue SET state='PAUSED_AUTH', paused_until=:until WHERE queue_key='u1'"),
        {"until": NOW + timedelta(minutes=5)},
    )
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is False


def _seed_ready_task(db_session: Session, task_id: str, queue_key: str) -> None:
    db_session.execute(
        text(
            "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
            "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
            "env_topology, git_context, created_at) VALUES "
            "(:sid,'p','b',:sid,'d','v',:u,'SSD',1,'RUNNING','{}','{}',now())"
        ),
        {"sid": f"S-{task_id}", "u": queue_key},
    )
    db_session.execute(
        text(
            "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
            "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
            "max_attempts, lease_generation, lease_until, callback_status, payload, "
            "created_at, updated_at) VALUES "
            "(:tid,'b',:sid,'R1',1,'p',:sid,'case_ai',:u,'READY',1,0,3,0,'epoch','PENDING',"
            "'{}',now(),now())"
        ),
        {"tid": task_id, "sid": f"S-{task_id}", "u": queue_key},
    )


def test_ai_claim_sets_running_and_slot_in_one_transaction(
    db_session: Session, db_engine: Engine
) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    _seed_ready_task(db_session, "T1", "u1")
    db_session.commit()

    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None and row.task_id == "T1"
    assert row.queue_state == QueueState.AI_RUNNING
    assert row.dispatch_phase == "JIT_FETCH"
    with db_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT in_flight FROM user_queue WHERE queue_key='u1'")).scalar_one()
            == 1
        )
        assert (
            conn.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one()
            == "AI_RUNNING"
        )


def test_never_dispatched_queue_wins_fairness_round(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("busy", NOW)
    repo.ensure_user_queue("fresh", NOW)
    db_session.execute(
        text("UPDATE user_queue SET last_dispatch_at=:t WHERE queue_key='busy'"), {"t": NOW}
    )
    _seed_ready_task(db_session, "T-old", "busy")
    _seed_ready_task(db_session, "T-new", "fresh")
    db_session.commit()
    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None
    assert row.task_id == "T-new"


def test_reclaim_confirmed_returns_ready_and_releases_slot(
    db_session: Session, db_engine: Engine
) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    _seed_ready_task(db_session, "T1", "u1")
    db_session.commit()
    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None
    db_session.execute(
        text("UPDATE task_queue SET lease_until=:past WHERE task_id='T1'"),
        {"past": NOW - timedelta(seconds=1)},
    )
    db_session.commit()
    assert repo.reclaim_confirmed("T1", "worker-a", row.lease_generation, NOW) is True
    db_session.commit()
    with db_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one()
            == "READY"
        )
        assert (
            conn.execute(text("SELECT in_flight FROM user_queue WHERE queue_key='u1'")).scalar_one()
            == 0
        )
