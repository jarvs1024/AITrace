# tests/unit/test_notify.py
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.notify.cards import NotifyConfig, build_card, detect_high_risk
from aitrace.notify.worker import NotifyWorker
from aitrace.persistence.models import TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

_NOW = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


class _Poster:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, webhook: str, card: dict[str, Any]) -> None:
        self.calls.append((webhook, card))

    @property
    def webhooks(self) -> list[str]:
        return [webhook for webhook, _ in self.calls]

    @property
    def texts(self) -> list[str]:
        return [card["markdown"]["text"] for _, card in self.calls]


def _config(
    webhook: str = "https://hook.example/ding",
    *,
    at_users: list[str] | None = None,
    high_risk_webhook: str | None = None,
) -> NotifyConfig:
    return NotifyConfig(
        webhook=webhook, at_users=at_users or [], high_risk_webhook=high_risk_webhook
    )


def _enqueue_notify(
    db: Session,
    queue: QueueRepository,
    *,
    task_id: str,
    event: dict[str, Any],
    batch_id: str = "B1",
    product_id: str = "ssd_9560",
    max_attempts: int = 3,
    enqueue_at: datetime = _NOW,
) -> None:
    queue.enqueue(
        NewTask(
            task_id=task_id,
            batch_id=task_id,
            session_id=None,
            round_id="",
            round_no=0,
            product_id=product_id,
            case_id="",
            task_type=TaskType.NOTIFY,
            queue_key=batch_id,
            max_attempts=max_attempts,
            payload={"batch_id": batch_id, "product_id": product_id, **event},
        ),
        enqueue_at,
    )
    db.commit()


def _batch_card(case_id: str, category: str = "FW_DEFECT", confidence: float = 0.8) -> dict:
    return {
        "event": "batch_card",
        "batch_id": "B1",
        "session_id": f"s-{case_id}",
        "case_id": case_id,
        "category": category,
        "detail": f"{case_id} 根因细节",
        "confidence": confidence,
    }


# ---------- build_card 纯函数 ----------


def test_build_card_batch_card_counts_conclusions_and_at_users() -> None:
    card = build_card(
        "batch_card",
        batch_id="B1",
        product_id="ssd_9560",
        counts={"completed": 2},
        conclusions=[
            {"case_id": "C1", "category": "FW_DEFECT", "confidence": 0.8},
            {"case_id": "C2", "category": "TEST_ENV_ERROR", "confidence": None},
        ],
        at_users=["zhangsan"],
    )
    assert card["msgtype"] == "markdown"
    text = card["markdown"]["text"]
    assert "已出结论：2 例" in text
    assert "案例 C1：FW_DEFECT（置信度 0.8）" in text
    assert "案例 C2：TEST_ENV_ERROR（置信度 —）" in text
    assert card["at"] == {"atMobiles": ["zhangsan"], "isAtAll": False}
    assert "ssd_9560" in card["markdown"]["title"]


def test_build_card_high_risk_keywords_and_detail() -> None:
    card = build_card(
        "high_risk",
        batch_id="B1",
        product_id="ssd_9560",
        case_id="C9",
        keywords=["AER", "CXL Link Down"],
        detail="PCIe AER 致命错误",
    )
    title = card["markdown"]["title"]
    text = card["markdown"]["text"]
    assert "高危" in title
    assert "命中关键词：AER、CXL Link Down" in text
    assert "PCIe AER 致命错误" in text
    assert "at" not in card


def test_build_card_final_status_counts_and_negated() -> None:
    card = build_card(
        "final",
        batch_id="B1",
        product_id="ssd_9560",
        counts={"VALID": 1, "INVALID": 0, "AWAITING_FEEDBACK": 2, "FAILED": 1, "RUNNING": 0},
        conclusions=[{"case_id": "C1", "category": "FW_DEFECT", "confidence": 0.8}],
        negated=["同批引用的首例结论已被否定"],
        batch_state="COMPLETED",
    )
    text = card["markdown"]["text"]
    assert "终态汇总" in card["markdown"]["title"]
    assert "批次状态：COMPLETED" in text
    assert "状态 VALID：1 例" in text
    assert "状态 AWAITING_FEEDBACK：2 例" in text
    assert "否定追加：同批引用的首例结论已被否定" in text


def test_build_card_negated_append_lines() -> None:
    card = build_card(
        "negated",
        batch_id="B1",
        product_id="ssd_9560",
        negated=["同批引用的首例结论已被否定，本案例引用注记已标 CITING_STALE"],
    )
    text = card["markdown"]["text"]
    assert "结论被否定" in card["markdown"]["title"]
    assert "同批引用的首例结论已被否定，本案例引用注记已标 CITING_STALE" in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("固件断言 FW ASSERT 触发", ["FW ASSERT"]),
        ("PCIe AER 致命错误", ["AER"]),
        ("检测到 CXL link down", ["CXL Link Down"]),
        ("普通脚本超时", []),
    ],
)
def test_detect_high_risk(text: str, expected: list[str]) -> None:
    assert detect_high_risk(text) == expected


# ---------- NotifyWorker（假 fetcher/poster + 真实队列） ----------


def test_worker_aggregates_batch_cards_across_ticks(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster = _Poster()
    worker = NotifyWorker(
        queue, config_fetcher=lambda product_id: _config(), poster=poster, clock=_Clock(_NOW)
    )
    _enqueue_notify(db_session, queue, task_id="T-1", event=_batch_card("C1", confidence=0.8))
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    first = poster.texts[0]
    assert "已出结论：1 例" in first
    assert "案例 C1" in first
    _enqueue_notify(
        db_session,
        queue,
        task_id="T-2",
        event=_batch_card("C2", confidence=0.6),
        enqueue_at=_NOW + timedelta(seconds=1),
    )
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    second = poster.texts[1]
    assert "已出结论：2 例" in second
    assert "案例 C2" in second


def test_worker_negated_appends_to_next_aggregated_card(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster = _Poster()
    worker = NotifyWorker(
        queue, config_fetcher=lambda product_id: _config(), poster=poster, clock=_Clock(_NOW)
    )
    _enqueue_notify(db_session, queue, task_id="T-1", event=_batch_card("C1"))
    _enqueue_notify(
        db_session,
        queue,
        task_id="T-2",
        event={
            "event": "negated",
            "citing_session_id": "s-C1",
            "citing_round_no": 1,
            "source_session_id": "s-leader",
            "message": "同批引用的首例结论已被否定",
        },
        enqueue_at=_NOW + timedelta(seconds=1),
    )
    _enqueue_notify(
        db_session,
        queue,
        task_id="T-3",
        event=_batch_card("C2"),
        enqueue_at=_NOW + timedelta(seconds=2),
    )
    worker.run_once(db_session, queue, _NOW, owner="w-1")
    worker.run_once(db_session, queue, _NOW, owner="w-1")
    worker.run_once(db_session, queue, _NOW, owner="w-1")
    assert "同批引用的首例结论已被否定" in poster.texts[1]
    assert "否定追加：同批引用的首例结论已被否定" in poster.texts[2]


def test_worker_config_cache_ttl(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster = _Poster()
    clock = _Clock(_NOW)
    fetcher_calls: list[str] = []

    def fetcher(product_id: str) -> NotifyConfig:
        fetcher_calls.append(product_id)
        return _config()

    worker = NotifyWorker(queue, config_fetcher=fetcher, poster=poster, cache_ttl=300, clock=clock)
    for i in range(3):
        _enqueue_notify(db_session, queue, task_id=f"T-{i}", event=_batch_card(f"C{i}"))
        worker.run_once(db_session, queue, clock.now(), owner="w-1")
        clock.advance(100)
    assert fetcher_calls == ["ssd_9560"]
    _enqueue_notify(db_session, queue, task_id="T-9", event=_batch_card("C9"))
    clock.advance(200)
    worker.run_once(db_session, queue, clock.now(), owner="w-1")
    assert fetcher_calls == ["ssd_9560", "ssd_9560"]


def test_worker_fetcher_failure_falls_back_to_default_webhook(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster = _Poster()

    def fetcher(product_id: str) -> NotifyConfig:
        raise RuntimeError("notify-config 网络不可达")

    worker = NotifyWorker(
        queue,
        config_fetcher=fetcher,
        poster=poster,
        default_webhook="https://fallback.example/hook",
        clock=_Clock(_NOW),
    )
    _enqueue_notify(db_session, queue, task_id="T-1", event=_batch_card("C1"))
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    assert poster.webhooks == ["https://fallback.example/hook"]
    row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "T-1")).scalar_one()
    assert row.queue_state == QueueState.DONE.value


def test_worker_poster_failure_fails_task_without_raising(db_session: Session) -> None:
    queue = QueueRepository(db_session)

    def poster(webhook: str, card: dict[str, Any]) -> None:
        raise RuntimeError("钉钉 webhook 拒绝连接")

    worker = NotifyWorker(
        queue,
        config_fetcher=lambda product_id: _config(),
        poster=poster,
        clock=_Clock(_NOW),
    )
    _enqueue_notify(db_session, queue, task_id="T-1", event=_batch_card("C1"), max_attempts=1)
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 0
    row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "T-1")).scalar_one()
    assert row.queue_state == QueueState.FAILED.value
    assert row.attempts == 1
    assert row.error is not None


def test_worker_requeues_with_backoff_until_max_attempts(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster_calls = 0

    def poster(webhook: str, card: dict[str, Any]) -> None:
        nonlocal poster_calls
        poster_calls += 1
        raise RuntimeError("poster 失败")

    worker = NotifyWorker(
        queue,
        config_fetcher=lambda product_id: _config(),
        poster=poster,
        clock=_Clock(_NOW),
    )
    _enqueue_notify(db_session, queue, task_id="T-1", event=_batch_card("C1"), max_attempts=3)
    run_count = 0
    now = _NOW
    row = None
    while run_count < 3:
        run_count += 1
        worker.run_once(db_session, queue, now, owner="w-1")
        db_session.expire_all()
        row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "T-1")).scalar_one()
        now = row.next_retry_at or now
    assert poster_calls == 3
    assert row is not None
    assert row.attempts == 3
    assert row.queue_state == QueueState.FAILED.value


def test_worker_uses_high_risk_webhook_for_high_risk_event(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    poster = _Poster()
    worker = NotifyWorker(
        queue,
        config_fetcher=lambda product_id: _config(high_risk_webhook="https://high.example/hook"),
        poster=poster,
        clock=_Clock(_NOW),
    )
    _enqueue_notify(
        db_session,
        queue,
        task_id="T-1",
        event={
            **_batch_card("C1"),
            "high_risk": {"keywords": ["AER"], "detail": "PCIe AER 致命错误"},
        },
    )
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    assert poster.webhooks == ["https://high.example/hook", "https://high.example/hook"]
    assert "高危" in poster.calls[1][1]["markdown"]["title"]
