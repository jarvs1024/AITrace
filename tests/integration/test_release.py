# tests/integration/test_release.py
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.settings import AISettings
from aitrace.persistence.models import Batch, Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.release import sweep_waiting

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed_group(
    db_session,
    *,
    leader_state: str,
    leader_round_status: str = "AWAITING_FEEDBACK",
    leader_confidence: float | None = 0.85,
    leader_category: str = "FW_DEFECT",
    follower_wait_ago: timedelta = timedelta(seconds=0),
) -> tuple[TaskQueue, TaskQueue]:
    db_session.add(
        Batch(
            batch_id="B1",
            product_id="ssd_9560",
            trigger_user_id="zhangsan",
            batch_task_id="T-BATCH-B1",
            failed_cases=[{"case_id": "C0"}, {"case_id": "C1"}],
            passed_cases=[],
            gate={"ai_used": 1},
            updated_at=_NOW,
        )
    )
    leader = _session(db_session, "s0", "fp-a", QueueState.READY.value, TaskType.CASE_AI)
    follower = _session(
        db_session,
        "s1",
        "fp-a",
        QueueState.WAITING.value,
        TaskType.CASE_CLEAN,
        depends=leader.task_id,
        updated_at=_NOW - follower_wait_ago,
    )
    leader.queue_state = leader_state
    round_row = db_session.execute(select(Round).where(Round.session_id == "s0")).scalar_one()
    round_row.round_status = leader_round_status
    round_row.confidence = leader_confidence
    if leader_category is not None:
        report = Report(
            report_id="rp-0",
            round_id=round_row.round_id,
            fault_info={},
            result={
                "root_cause": {"category": leader_category, "detail": "d", "evidence": []},
                "solution": None,
                "confidence": leader_confidence,
                "gap_note": None,
            },
            confidence_breakdown=[],
            evidence=[],
            referenced_logs=[],
            process_steps=[],
            citation={"citing_status": "NONE"},
            gap_note=None,
            created_at=_NOW,
        )
        db_session.add(report)
        round_row.report_id = "rp-0"
    db_session.flush()
    return leader, follower


def _session(db_session, sid, fp, state, task_type, depends=None, updated_at=_NOW) -> TaskQueue:
    db_session.add(
        SessionModel(
            session_id=sid,
            product_id="ssd_9560",
            batch_id="B1",
            case_id=sid.upper(),
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            stable_fingerprint=fp,
            env_topology={},
            git_context={},
            created_at=_NOW,
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
    task = TaskQueue(
        task_id=f"task-{sid}",
        batch_id="B1",
        session_id=sid,
        round_id=f"round-{sid}",
        round_no=1,
        product_id="ssd_9560",
        case_id=sid.upper(),
        task_type=task_type,
        queue_key="zhangsan",
        queue_state=state,
        depends_on_task_id=depends,
        created_at=_NOW,
        updated_at=updated_at,
    )
    db_session.add(task)
    db_session.flush()
    return task


def test_cite_path_writes_citation_report(db_session) -> None:
    queue = QueueRepository(db_session)
    _leader, follower = _seed_group(db_session, leader_state=QueueState.DONE.value)
    swept = sweep_waiting(db_session, queue, _Ids(), _NOW, AISettings())
    assert swept == 1
    db_session.refresh(follower)
    assert follower.queue_state == QueueState.DONE.value
    follower_round = db_session.execute(select(Round).where(Round.session_id == "s1")).scalar_one()
    assert follower_round.round_status == "AWAITING_FEEDBACK"
    assert follower_round.confidence == 0.85
    report = db_session.execute(
        select(Report).where(Report.round_id == follower_round.round_id)
    ).scalar_one()
    assert report.citation["citing_status"] == "CITING"
    assert report.citation["source_session"] == "s0"
    assert report.result["root_cause"]["category"] == "FW_DEFECT"


def test_no_threshold_promotes_follower(db_session) -> None:
    queue = QueueRepository(db_session)
    _leader, follower = _seed_group(
        db_session, leader_state=QueueState.DONE.value, leader_confidence=0.5
    )
    swept = sweep_waiting(db_session, queue, _Ids(), _NOW, AISettings())
    assert swept == 1
    db_session.refresh(follower)
    assert follower.queue_state == QueueState.READY.value
    assert follower.task_type == TaskType.CASE_AI
    assert follower.depends_on_task_id is None


def test_no_threshold_and_budget_exhausted_rules_other(db_session) -> None:
    queue = QueueRepository(db_session)
    _leader, follower = _seed_group(
        db_session, leader_state=QueueState.DONE.value, leader_confidence=0.5
    )
    batch = db_session.execute(select(Batch)).scalar_one()
    batch.gate = {"ai_used": 3}
    db_session.flush()
    swept = sweep_waiting(db_session, queue, _Ids(), _NOW, AISettings(batch_ai_budget=3))
    assert swept == 1
    db_session.refresh(follower)
    assert follower.queue_state == QueueState.DONE.value
    follower_round = db_session.execute(select(Round).where(Round.session_id == "s1")).scalar_one()
    assert follower_round.report_id is not None
    report = db_session.execute(
        select(Report).where(Report.round_id == follower_round.round_id)
    ).scalar_one()
    assert report.result["root_cause"]["category"] == "OTHER"


def test_leader_failed_promotes_next(db_session) -> None:
    queue = QueueRepository(db_session)
    _leader, follower = _seed_group(
        db_session,
        leader_state=QueueState.FAILED.value,
        leader_round_status="FAILED",
        leader_confidence=None,
        leader_category=None,
    )
    swept = sweep_waiting(db_session, queue, _Ids(), _NOW, AISettings())
    assert swept == 1
    db_session.refresh(follower)
    assert follower.queue_state == QueueState.READY.value
    assert follower.task_type == TaskType.CASE_AI
    assert follower.depends_on_task_id is None


def test_waiting_timeout_rules_other(db_session) -> None:
    queue = QueueRepository(db_session)
    _leader, follower = _seed_group(
        db_session,
        leader_state=QueueState.AI_RUNNING.value,
        follower_wait_ago=timedelta(seconds=2000),
    )
    swept = sweep_waiting(db_session, queue, _Ids(), _NOW, AISettings(waiting_timeout_seconds=1800))
    assert swept == 1
    db_session.refresh(follower)
    assert follower.queue_state == QueueState.DONE.value
    follower_round = db_session.execute(select(Round).where(Round.session_id == "s1")).scalar_one()
    report = db_session.execute(
        select(Report).where(Report.round_id == follower_round.round_id)
    ).scalar_one()
    assert report.result["root_cause"]["category"] == "OTHER"
    assert "等待超时" in report.result["root_cause"]["detail"]
