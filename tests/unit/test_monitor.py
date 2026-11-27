from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from aitrace.ai.token_client import TokenFetchClient
from aitrace.core.enums import QueueState, TaskType
from aitrace.core.settings import Settings
from aitrace.observability.metrics import MetricsRegistry
from aitrace.observability.monitor import build_monitor_snapshot
from aitrace.persistence.models import TaskQueue, UserQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

_TERMINAL_STATES = {QueueState.DONE, QueueState.FAILED}


def _seed_task(
    db: Session,
    *,
    task_id: str,
    queue_key: str,
    state: QueueState,
    updated_at: datetime,
    task_type: TaskType = TaskType.CASE_AI,
) -> None:
    db.add(
        TaskQueue(
            task_id=task_id,
            batch_id=f"B-{task_id}",
            session_id=None,
            round_id=f"R-{task_id}",
            round_no=0,
            product_id="ssd_9560",
            case_id="",
            task_type=str(task_type),
            queue_key=queue_key,
            queue_state=str(state),
            created_at=updated_at,
            updated_at=updated_at,
        )
    )


def _all_waits(waits: dict[str, dict[str, list[float]]], queue_key: str) -> list[float]:
    found: list[float] = []
    for bucket in waits.values():
        found.extend(bucket.get(queue_key, []))
    return found


def _snapshot(db: Session, settings: Settings) -> dict:
    return build_monitor_snapshot(db, MetricsRegistry(), settings, now=datetime.now(UTC))


def test_snapshot_contains_required_sections(db_session: Session, test_settings: Settings) -> None:
    snap = _snapshot(db_session, test_settings)
    for key in (
        "generated_at",
        "runtime",
        "waits",
        "fairness",
        "alerts_eligible",
        "watermark",
        "callbacks",
        "workers",
        "audit",
    ):
        assert key in snap
    assert snap["watermark"]["level"] in ("OK", "WARN", "FORCE")
    assert snap["audit"]["verified"] is None  # 无审计服务注入时不得伪造通过


def test_ready_wait_computed_from_updated_at(db_session: Session, test_settings: Settings) -> None:
    now = datetime.now(UTC)
    _seed_task(
        db_session,
        task_id="T-WAIT-1",
        queue_key="zhangsan",
        state=QueueState.READY,
        updated_at=now - timedelta(seconds=130),
    )
    db_session.commit()
    snap = build_monitor_snapshot(db_session, MetricsRegistry(), test_settings, now=now)
    waits = _all_waits(snap["waits"], "zhangsan")
    assert waits, f"zhangsan 的等待未出现在快照：{snap['waits']}"
    assert pytest.approx(waits[0], abs=2.0) == 130.0


def test_waits_bucketed_by_ready_depth(db_session: Session, test_settings: Settings) -> None:
    now = datetime.now(UTC)
    # zhangsan 有 5 个 READY 前序 → 进入 bucket_gt4；lisi 无 READY → bucket_0
    for i in range(5):
        _seed_task(
            db_session,
            task_id=f"T-ZS-{i}",
            queue_key="zhangsan",
            state=QueueState.READY,
            updated_at=now - timedelta(seconds=20 + i),
        )
    _seed_task(
        db_session,
        task_id="T-LS-0",
        queue_key="lisi",
        state=QueueState.PENDING,
        updated_at=now - timedelta(seconds=300),
    )
    db_session.commit()
    snap = build_monitor_snapshot(db_session, MetricsRegistry(), test_settings, now=now)
    assert snap["waits"]["bucket_0"]["lisi"]
    assert "zhangsan" not in snap["waits"]["bucket_0"]
    assert snap["waits"]["bucket_gt4"]["zhangsan"]


def test_fairness_gap_is_p95_difference(db_session: Session, test_settings: Settings) -> None:
    now = datetime.now(UTC)
    for i, wait in enumerate((10, 190)):
        _seed_task(
            db_session,
            task_id=f"T-FA-{i}",
            queue_key="user-a",
            state=QueueState.READY,
            updated_at=now - timedelta(seconds=wait),
        )
    for i, wait in enumerate((20, 40)):
        _seed_task(
            db_session,
            task_id=f"T-FB-{i}",
            queue_key="user-b",
            state=QueueState.READY,
            updated_at=now - timedelta(seconds=wait),
        )
    db_session.commit()
    snap = build_monitor_snapshot(db_session, MetricsRegistry(), test_settings, now=now)
    bucket = snap["fairness"]["bucket_1_2"]
    assert set(bucket["queue_keys"]) == {"user-a", "user-b"}
    assert pytest.approx(bucket["gap_seconds"], abs=2.0) == 150.0


def test_runtime_reports_paused_auth_and_ai_workers(
    db_session: Session, test_settings: Settings
) -> None:
    now = datetime.now(UTC)
    db_session.add(
        UserQueue(
            queue_key="u-paused",
            state="PAUSED_AUTH",
            updated_at=now - timedelta(seconds=660),
        )
    )
    _seed_task(
        db_session,
        task_id="T-AI-RUN",
        queue_key="u-active",
        state=QueueState.AI_RUNNING,
        updated_at=now,
    )
    db_session.commit()
    snap = build_monitor_snapshot(db_session, MetricsRegistry(), test_settings, now=now)
    paused = snap["runtime"]["paused_auth"]
    assert paused["count"] == 1
    assert pytest.approx(paused["sustained_seconds"], abs=2.0) == 660.0
    workers = snap["workers"]
    assert workers["ai_running"] == 1
    assert workers["width_w"] == test_settings.queue.ai_worker_concurrency
    assert workers["utilization"] == pytest.approx(1.0 / workers["width_w"])


def test_snapshot_contains_no_sensitive_values(
    db_session: Session, test_settings: Settings
) -> None:
    snap = _snapshot(db_session, test_settings)
    blob = str(snap)
    assert "test-service-token" not in blob
    assert "secret" not in blob.lower()


def test_queue_finish_metrics_and_fencing_reject(
    db_session: Session, test_settings: Settings
) -> None:
    registry = MetricsRegistry()
    queue = QueueRepository(db_session, metrics=registry)
    now = datetime.now(UTC)
    row = queue.enqueue(
        NewTask(
            task_id="T-FIN-1",
            batch_id="B-FIN-1",
            session_id=None,
            round_id="R-FIN-1",
            round_no=1,
            product_id="ssd_9560",
            case_id="CASE-1",
            task_type=TaskType.CASE_CLEAN,
            queue_key="u1",
        ),
        now,
    )
    db_session.commit()
    claimed = queue.claim_clean("owner-x", now)
    assert claimed is not None
    assert queue.finish(row.task_id, "owner-x", 0, QueueState.DONE, now) is False
    assert queue.finish(row.task_id, "owner-x", 1, QueueState.DONE, now) is True
    db_session.commit()
    snap = registry.snapshot()
    assert snap["task_finish_total:DONE"] == 1
    assert snap["fencing_reject_total"] == 1


def test_token_fetch_records_ok_and_fail_metrics(test_settings: Settings) -> None:
    registry = MetricsRegistry()
    grant = {"token": "t-123", "expires_at": 4102444800}
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=grant)
        return httpx.Response(500, json={"code": "UPSTREAM_DOWN", "message": "上游故障"})

    client = TokenFetchClient(
        base_url="http://mock",
        read_token="rt",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
        registry=registry,
    )
    first = client.fetch(
        product_id="p1", batch_id="b1", case_id="c1", trigger_user_id="u1", action="analyze"
    )
    assert first.token == "t-123"
    with pytest.raises(Exception, match="令牌获取失败"):
        client.fetch(
            product_id="p1", batch_id="b1", case_id="c1", trigger_user_id="u1", action="analyze"
        )
    snap = registry.snapshot()
    assert snap["token_fetch_ok_total"] == 1
    assert snap["token_fetch_fail_total"] == 1
    assert snap["token_fetch_fail:count_300s"] == 1
