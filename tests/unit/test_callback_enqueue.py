from datetime import UTC, datetime

from sqlalchemy import select

from aitrace.callback.enqueue import enqueue_progress, enqueue_queue_event, enqueue_result
from aitrace.core.enums import Stage, TaskType
from aitrace.persistence.models import SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed_session(
    db_session, session_id: str, *, batch_id: str = "B1", case_id: str = "C1"
) -> None:
    # task_queue.session_id 有 FK → session，且 uq_task_case 对 case_id<>'' 唯一。
    db_session.add(
        SessionModel(
            session_id=session_id,
            product_id="ssd_9560",
            batch_id=batch_id,
            case_id=case_id,
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            latest_round_no=1,
            status="RUNNING",
            env_topology={},
            git_context={},
            created_at=datetime(2026, 11, 26, tzinfo=UTC),
        )
    )
    db_session.flush()


def _task(db_session, sid="s1") -> TaskQueue:
    # 上游任务行（case_ai）：其回调将另写一条 CALLBACK 任务，避免与 uq_task_case 冲突。
    _seed_session(db_session, sid)
    row = TaskQueue(
        task_id=f"task-{sid}",
        batch_id="B1",
        session_id=sid,
        round_id=f"round-{sid}",
        round_no=1,
        product_id="ssd_9560",
        case_id="C1" if sid != "s1" else "",
        task_type=TaskType.CASE_AI,
        queue_key="zhangsan",
        created_at=datetime(2026, 11, 26, tzinfo=UTC),
        updated_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_enqueue_progress_payload_shape(db_session) -> None:
    queue = QueueRepository(db_session)
    task = _task(db_session, "s9")
    enqueue_progress(
        db_session,
        queue,
        _Ids(),
        task=task,
        stage=Stage.AI_CLASSIFYING,
        progress=50,
        waiting_reason=None,
        message="Qoder 主引擎正在全局归类并定界...",
        now=datetime(2026, 11, 26, tzinfo=UTC),
    )
    row = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()[-1]
    )
    payload = row.payload
    assert payload["event"] == "PROGRESS"
    assert payload["session_id"] == "s9"
    assert payload["stage"] == "AI_CLASSIFYING"
    assert payload["progress"] == 50
    assert payload["seq"] >= 1
    assert payload["batch_seq"] >= 1
    assert payload["report"] is None


def test_enqueue_result_carries_full_report(db_session) -> None:
    queue = QueueRepository(db_session)
    task = _task(db_session, "s9")
    enqueue_result(
        db_session,
        queue,
        _Ids(),
        task=task,
        report_payload={"root_cause": {"category": "FW_DEFECT"}},
        citation=None,
        now=datetime(2026, 11, 26, tzinfo=UTC),
    )
    rows = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    result_row = rows[-1]
    payload = result_row.payload
    assert payload["event"] == "RESULT"
    assert payload["stage"] == "COMPLETED"
    assert payload["progress"] == 100
    assert payload["report"]["root_cause"]["category"] == "FW_DEFECT"
    assert result_row.priority <= 10  # 终态回调优先


def test_enqueue_queue_event_once_per_state_change(db_session) -> None:
    queue = QueueRepository(db_session)
    _seed_session(db_session, "", batch_id="", case_id="")
    enqueue_queue_event(
        db_session,
        queue,
        _Ids(),
        queue_key="zhangsan",
        event_type="PAUSED_AUTH",
        reason="TOKEN_EXPIRED",
        affected_tasks=["t1", "t2"],
        count=2,
        paused_until=datetime(2026, 11, 26, tzinfo=UTC),
        now=datetime(2026, 11, 26, tzinfo=UTC),
    )
    rows = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    payload = rows[-1].payload
    assert payload["event"] == "QUEUE_STATE"
    assert payload["queue_event"]["type"] == "PAUSED_AUTH"
    assert payload["queue_event"]["affected_tasks"] == ["t1", "t2"]
    assert payload["queue_event"]["count"] == 2
