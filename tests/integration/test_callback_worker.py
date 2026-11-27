from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from aitrace.callback.worker import CallbackWorker
from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.models import SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository

_NOW = datetime(2026, 11, 26, tzinfo=UTC)


def _seed_session(db_session) -> None:
    # task_queue.session_id 有 FK → session。
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


def _enqueue(db_session, queue) -> None:
    _seed_session(db_session)
    queue.enqueue(
        type(
            "T",
            (),
            {
                "task_id": "task-cb-1",
                "batch_id": "B1",
                "session_id": "s1",
                "round_id": "r1",
                "round_no": 1,
                "product_id": "ssd_9560",
                "case_id": "C1",
                "task_type": TaskType.CALLBACK,
                "queue_key": "zhangsan",
                "priority": 20,
                "max_attempts": 3,
                "payload": {
                    "event": "RESULT",
                    "session_id": "s1",
                    "batch_id": "B1",
                    "case_id": "C1",
                    "round_no": 1,
                    "seq": 1,
                    "batch_seq": 1,
                    "stage": "COMPLETED",
                    "progress": 100,
                    "waiting_reason": None,
                    "message": "知因分析完成（点击查看根因报告）",
                    "report": {"root_cause": {"category": "FW_DEFECT"}},
                    "citation": None,
                    "queue_event": None,
                    "sessions": None,
                },
            },
        )(),
        _NOW,
    )


def _client_factory(status_codes: list[int], seen: list[dict]) -> Callable:
    def factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append({"url": str(request.url), "body": request.content.decode("utf-8")})
            code = status_codes.pop(0) if status_codes else 200
            return httpx.Response(code, json={"ok": True})

        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    return factory


def test_callback_worker_posts_and_finishes_done(db_session) -> None:
    queue = QueueRepository(db_session)
    _enqueue(db_session, queue)
    seen: list[dict] = []
    worker = CallbackWorker(
        client_factory=_client_factory([200], seen),
        base_url="http://mock-aitest",
        read_token="secret",
    )
    count = worker.run_once(db_session, _NOW, "cb-worker-1")
    assert count == 1
    row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "task-cb-1")).scalar_one()
    assert row.queue_state == QueueState.DONE.value
    assert row.callback_status == "SENT"
    assert seen and seen[0]["url"].endswith("/api/v1/aitest/aitrace/status")
    assert "FW_DEFECT" in seen[0]["body"]


def test_callback_worker_retries_on_failure_then_fails(db_session) -> None:
    queue = QueueRepository(db_session)
    _enqueue(db_session, queue)
    seen: list[dict] = []
    worker = CallbackWorker(
        client_factory=_client_factory([500, 500, 500, 500], seen),
        base_url="http://mock-aitest",
        read_token="secret",
    )
    # run_once 不 commit（会话由调用方管理）；retry_callback 写指数退避（30s 起），
    # 每次把时钟拨到最大退避（1800s）之上，保证下一轮可重新 claim。
    now = _NOW
    for _ in range(4):
        now = now + timedelta(seconds=2000)
        worker.run_once(db_session, now, "cb-worker-1")
        db_session.commit()
    row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "task-cb-1")).scalar_one()
    assert row.queue_state == QueueState.FAILED.value
    assert row.callback_status == "FAILED"
    assert row.attempts == 3
