from datetime import UTC, datetime

from sqlalchemy import select, update

from aitrace.core.enums import QueueState, Stage
from aitrace.persistence.models import Round, SessionModel, TaskQueue
from aitrace.scheduling.aggregate import (
    compute_batch_groups,
    promote_on_leader_failed,
    suspend_followers,
)

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _session(db_session, sid, fp, batch="B1") -> SessionModel:
    row = SessionModel(
        session_id=sid,
        product_id="ssd_9560",
        batch_id=batch,
        case_id=sid,
        domain_id="enterprise",
        profile_version="v1",
        trigger_user_id="zhangsan",
        hardware_type="SSD",
        stable_fingerprint=fp,
        env_topology={},
        git_context={},
        created_at=_NOW,
    )
    db_session.add(row)
    db_session.flush()
    db_session.add(
        Round(
            round_id=f"round-{sid}",
            session_id=sid,
            round_no=1,
            trigger_user_id="zhangsan",
            queue_key="zhangsan",
            stage=Stage.QUEUED.value,
            round_status="RUNNING",
            started_at=_NOW,
        )
    )
    db_session.add(
        TaskQueue(
            task_id=f"task-{sid}",
            batch_id=batch,
            session_id=sid,
            round_id=f"round-{sid}",
            round_no=1,
            product_id="ssd_9560",
            case_id=sid,
            task_type="case_clean",
            queue_key="zhangsan",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return row


def test_suspend_followers_sets_waiting_and_dependency(db_session) -> None:
    _session(db_session, "s1", "fp-a")
    _session(db_session, "s2", "fp-a")
    db_session.flush()
    sessions = db_session.execute(select(SessionModel)).scalars().all()
    groups = compute_batch_groups(sessions)
    suspended = suspend_followers(db_session, groups, _NOW)
    assert suspended == 1
    # 跟随者任务置 WAITING 且依赖首例任务
    follower_task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == "s2")
    ).scalar_one_or_none()
    leader_task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == "s1")
    ).scalar_one_or_none()
    assert follower_task is not None and leader_task is not None
    assert follower_task.queue_state == QueueState.WAITING.value
    assert follower_task.depends_on_task_id == leader_task.task_id


def test_promotion_picks_next_member_when_leader_failed(db_session) -> None:
    _session(db_session, "s1", "fp-a")
    _session(db_session, "s2", "fp-a")
    db_session.flush()
    sessions = db_session.execute(select(SessionModel)).scalars().all()
    groups = compute_batch_groups(sessions)
    assert suspend_followers(db_session, groups, _NOW) == 1
    db_session.execute(update(Round).where(Round.session_id == "s1").values(round_status="FAILED"))
    db_session.flush()
    promoted = promote_on_leader_failed(db_session, groups, _NOW)
    assert promoted == 1
    follower_task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == "s2")
    ).scalar_one_or_none()
    assert follower_task is not None
    assert follower_task.queue_state == QueueState.READY.value
    assert follower_task.depends_on_task_id is None
