# tests/integration/test_ai_worker.py
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from aitrace.ai.worker import AIWorker
from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.ids import UuidIdGenerator
from aitrace.core.settings import AISettings, AitestSettings
from aitrace.persistence.models import (
    Batch,
    Profile,
    Report,
    Round,
    SessionModel,
    TaskQueue,
    UserQueue,
)
from aitrace.profiles.models import DomainProfile

_NOW = datetime(2026, 11, 26, tzinfo=UTC)

_PROFILE = DomainProfile(
    domain_id="enterprise",
    product_ids=("ssd_9560",),
    log_sources=(
        {"name": "test", "plugin": "test.txt"},
        {"name": "uart", "plugin": "uart.txt"},
    ),
    anchor_keywords={"uart": ("@TICK",)},
    analysis_focus="SSD 固件与硬件定界",
    skills_namespace="enterprise_",
    memory_namespace="domain:enterprise:",
    probe_whitelist=("nvme list", "nvme id-ctrl"),
)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _seed(
    db_session,
    tmp_path: Path,
    *,
    data_dir: Path | None = None,
    queue_event_session: bool = False,
) -> tuple[TaskQueue, Path]:
    base = data_dir or tmp_path / "data"
    (base / "evidence/s1/round_1").mkdir(parents=True)
    summary = {
        "status": "CLEANED",
        "source_availability": {"console": 1.0, "kernel": 1.0, "health": 0.5, "uart": 1.0},
        "first_error": {
            "ts_utc": "2026-11-26T10:00:00Z",
            "exc_type": "TimeoutException",
            "err_code": "0x1A",
            "line_no": 120,
            "excerpt": "command timeout after 30s",
        },
        "anchor_utc": "2026-11-26T10:00:00Z",
        "anchor_source": "uart",
        "relevant_sources": ["console", "uart", "kernel"],
    }
    metrics = {
        "duration_ms": 1200,
        "per_source": {
            "console": {"original_bytes": 1000, "slice_bytes": 800, "events_parsed": 10},
            "kernel": {"original_bytes": 2000, "slice_bytes": 1500, "events_parsed": 20},
            "health": {"original_bytes": 500, "slice_bytes": 500, "events_parsed": 5},
            "uart": {"original_bytes": 800, "slice_bytes": 600, "events_parsed": 8},
        },
        "alignment_rate": 0.95,
        "shrinks": 0,
        "exemptions": [],
    }
    (base / "evidence/s1/round_1/summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (base / "evidence/s1/round_1/metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    for name, content in [
        ("baseline.txt", "baseline\n"),
        ("error_window.txt", "ERROR uart timeout\n"),
        ("tail.txt", "tail\n"),
    ]:
        (base / "evidence/s1/round_1" / name).write_text(content, encoding="utf-8")
    db_session.add(
        Batch(
            batch_id="B1",
            product_id="ssd_9560",
            trigger_user_id="zhangsan",
            batch_task_id="T-BATCH-B1",
            failed_cases=[{"case_id": "C1"}],
            passed_cases=[],
            gate={"ai_used": 1},
            updated_at=_NOW,
        )
    )
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
            stable_fingerprint="fp-a",
            env_topology={},
            git_context={},
            created_at=_NOW,
        )
    )
    db_session.add(
        Profile(
            domain_id="enterprise",
            config=_PROFILE.model_dump(),
            version="v1",
            updated_by="test",
            updated_at=_NOW,
        )
    )
    if queue_event_session:
        # task_queue.session_id 有 FK → session；QUEUE_STATE 回调行 session_id=""
        # 需要占位 session 行（与 test_pause/test_callback_enqueue 同款模式）。
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
                env_topology={},
                git_context={},
                created_at=_NOW,
            )
        )
    db_session.flush()
    db_session.add(
        Round(
            round_id="r1",
            session_id="s1",
            round_no=1,
            trigger_user_id="zhangsan",
            queue_key="zhangsan",
            stage=Stage.CLEANING.value,
            round_status="RUNNING",
            started_at=_NOW,
        )
    )
    task = TaskQueue(
        task_id="task-ai-1",
        batch_id="B1",
        session_id="s1",
        round_id="r1",
        round_no=1,
        product_id="ssd_9560",
        case_id="C1",
        task_type=TaskType.CASE_AI,
        queue_key="zhangsan",
        queue_state=QueueState.READY.value,
        created_at=_NOW,
        updated_at=_NOW,
    )
    db_session.add(task)
    db_session.add(
        UserQueue(
            queue_key="zhangsan",
            state="ACTIVE",
            in_flight=0,
            paused_until=datetime(1970, 1, 1, tzinfo=UTC),
            last_error=None,
            last_dispatch_at=None,
            updated_at=_NOW,
        )
    )
    db_session.flush()
    return task, base


def _grant_transport(status: int = 200, body: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json=body
            if body is not None
            else {"code": "TOKEN_NOT_FOUND", "message": "用户未托管令牌", "retryable": False},
        )

    return httpx.MockTransport(handler)


def _ok_grant() -> dict:
    return {"token": "tok-abc", "expires_at": int(_NOW.timestamp()) + 3600}


def _worker(
    db_session,
    events: list[dict],
    *,
    grant: dict | None = None,
    token_status: int = 200,
    query_timeout: float = 60.0,
    data_dir: Path,
    settings=None,
) -> AIWorker:
    from aitrace.ai.qoder import FakeQoder

    settings = settings or AISettings(edition="fake")
    return AIWorker(
        settings=settings,
        aitest=AitestSettings(read_token="secret"),
        adapter=FakeQoder(events),
        token_transport=_grant_transport(token_status, grant or _ok_grant()),
        base_dir=data_dir,
        query_timeout=query_timeout,
        ids=UuidIdGenerator(),
    )


def _json_events(result: dict) -> list[dict]:
    return [
        {"kind": "delta", "text": "分析中"},
        {"kind": "result", "text": json.dumps(result, ensure_ascii=False)},
    ]


def _ok_result(slice_hash: str) -> dict:
    return {
        "root_cause_category": "FW_DEFECT",
        "root_cause_detail": "固件缺陷：命令超时路径缺少重试保护",
        "solution": "升级固件至 1.4.2 并重跑",
        "evidence": [{"source": "uart", "slice_hash": slice_hash}],
        "gap_note": None,
    }


def test_worker_full_success_path(db_session, tmp_path) -> None:
    task, base = _seed(db_session, tmp_path)
    window_hash = hashlib.sha256(
        (base / "evidence/s1/round_1/error_window.txt").read_bytes()
    ).hexdigest()
    worker = _worker(db_session, _json_events(_ok_result(window_hash)), data_dir=base)
    done = worker.run_once(db_session, _NOW, "ai-worker-1")
    assert done == 1
    db_session.refresh(task)
    assert task.queue_state == QueueState.DONE.value
    assert task.dispatch_phase is None
    round_row = db_session.execute(select(Round).where(Round.round_id == "r1")).scalar_one()
    assert round_row.round_status == "AWAITING_FEEDBACK"
    assert round_row.report_id is not None
    assert round_row.confidence is not None and round_row.confidence > 0
    report = db_session.get(Report, round_row.report_id)
    assert report.result["root_cause"]["category"] == "FW_DEFECT"
    assert report.citation == {"citing_status": "NONE"}
    assert len(report.confidence_breakdown) == 7
    session_row = db_session.execute(select(SessionModel)).scalar_one()
    assert session_row.status == "COMPLETED"
    # RESULT 回调入队
    cb = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    assert any(c.payload["event"] == "PROGRESS" for c in cb)
    assert any(c.payload["event"] == "RESULT" for c in cb)


def test_worker_jit_non_retryable_fails_task(db_session, tmp_path) -> None:
    task, base = _seed(db_session, tmp_path)
    worker = _worker(
        db_session,
        _json_events(_ok_result("any")),
        grant={"code": "TOKEN_NOT_FOUND", "message": "用户未托管令牌", "retryable": False},
        token_status=404,
        data_dir=base,
    )
    done = worker.run_once(db_session, _NOW, "ai-worker-1")
    assert done == 1
    db_session.refresh(task)
    assert task.queue_state == QueueState.FAILED.value
    assert task.error["code"] == "TOKEN_NOT_FOUND"
    round_row = db_session.execute(select(Round).where(Round.round_id == "r1")).scalar_one()
    assert round_row.round_status == "FAILED"
    assert round_row.stage == Stage.FAILED.value
    session_row = db_session.get(SessionModel, "s1")
    assert session_row is not None and session_row.status == "FAILED"


def test_worker_jit_retryable_pauses_and_retries(db_session, tmp_path) -> None:
    task, base = _seed(db_session, tmp_path, queue_event_session=True)
    worker = _worker(
        db_session,
        _json_events(_ok_result("any")),
        grant={"code": "TOKEN_EXPIRED", "message": "令牌已过期", "retryable": True},
        token_status=410,
        data_dir=base,
    )
    done = worker.run_once(db_session, _NOW, "ai-worker-1")
    assert done == 1
    db_session.refresh(task)
    assert task.queue_state == QueueState.READY.value
    assert task.attempts == 1
    assert task.next_retry_at is not None
    user_row = db_session.execute(select(UserQueue)).scalar_one()
    assert user_row.state == "PAUSED_AUTH"
    assert user_row.paused_until > _NOW
    assert json.loads(user_row.last_error)["code"] == "TOKEN_EXPIRED"
    # QUEUE_STATE 事件
    cb = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK))
        .scalars()
        .all()
    )
    assert any(c.payload["event"] == "QUEUE_STATE" for c in cb)


def test_worker_invalid_output_fails_task(db_session, tmp_path) -> None:
    task, base = _seed(db_session, tmp_path)
    bad = _json_events({"root_cause_category": "MADE_UP", "root_cause_detail": "x"})
    worker = _worker(db_session, bad, data_dir=base)
    done = worker.run_once(db_session, _NOW, "ai-worker-1")
    assert done == 1
    db_session.refresh(task)
    assert task.queue_state == QueueState.FAILED.value
    assert task.error["code"] == "VALIDATION_FAILED"
    assert "MADE_UP" in task.error["message"]
    assert "当前值" in task.error["message"]


def test_worker_authexpired_fenced_retry_and_pause(db_session, tmp_path) -> None:
    task, base = _seed(db_session, tmp_path, queue_event_session=True)
    events = [{"kind": "auth_expired"}]
    worker = _worker(db_session, events, data_dir=base)
    done = worker.run_once(db_session, _NOW, "ai-worker-1")
    assert done == 1
    db_session.refresh(task)
    assert task.queue_state == QueueState.READY.value
    user_row = db_session.execute(select(UserQueue)).scalar_one()
    assert user_row.state == "PAUSED_AUTH"
