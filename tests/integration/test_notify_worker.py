# tests/integration/test_notify_worker.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.knowledge.service import KnowledgeService
from aitrace.notify.cards import NotifyConfig
from aitrace.notify.worker import NotifyWorker
from aitrace.persistence.models import Batch, Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.batch_close import close_batch_if_done
from aitrace.scheduling.rule_report import write_rule_report
from aitrace.scheduling.types import NewTask

_NOW = datetime(2026, 11, 27, 9, 0, 0, tzinfo=UTC)


class _IdsSeq:
    def __init__(self) -> None:
        self._n = 0

    def new(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


class _Poster:
    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    def __call__(self, webhook: str, card: dict[str, Any]) -> None:
        self.cards.append(card)

    @property
    def texts(self) -> list[str]:
        return [card["markdown"]["text"] for card in self.cards]


class _Clock:
    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t


def _notify_tasks(db: Session) -> list[TaskQueue]:
    return list(
        db.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.NOTIFY)).scalars()
    )


def _worker(queue: QueueRepository, poster: _Poster) -> NotifyWorker:
    return NotifyWorker(
        queue,
        config_fetcher=lambda product_id: NotifyConfig(
            webhook="https://ding.example/hook", at_users=["zhangsan"], high_risk_webhook=None
        ),
        poster=poster,
        clock=_Clock(_NOW),
    )


# ---------- 触发点：批次关账 → final 卡片 ----------


def _seed_batch(db: Session, *, statuses: list[str]) -> None:
    db.add(
        Batch(
            batch_id="B1",
            product_id="ssd_9560",
            trigger_user_id="zhangsan",
            batch_task_id="T-BATCH-B1",
            failed_cases=[{"case_id": f"C{i}"} for i in range(len(statuses))],
            passed_cases=[],
            gate={},
            batch_state="RUNNING",
            updated_at=_NOW,
        )
    )
    for i, status in enumerate(statuses):
        sid = f"s{i}"
        db.add(
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
        db.flush()
        db.add(
            Round(
                round_id=f"r{i}",
                session_id=sid,
                round_no=1,
                trigger_user_id="zhangsan",
                queue_key="zhangsan",
                stage=Stage.COMPLETED.value,
                round_status=status,
                started_at=_NOW,
                finished_at=_NOW,
            )
        )
    db.flush()


def test_batch_close_enqueues_final_notify_and_worker_posts_summary(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    _seed_batch(db_session, statuses=["AWAITING_FEEDBACK", "VALID"])
    assert close_batch_if_done(db_session, queue, _IdsSeq(), _NOW) == 1
    tasks = _notify_tasks(db_session)
    assert len(tasks) == 1
    assert (tasks[0].payload or {})["event"] == "final"
    poster = _Poster()
    worker = _worker(queue, poster)
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    text = poster.texts[0]
    assert "终态汇总" in text
    assert "批次状态：COMPLETED" in text
    assert "状态 AWAITING_FEEDBACK：1 例" in text
    assert "状态 VALID：1 例" in text
    row = db_session.execute(
        select(TaskQueue).where(TaskQueue.task_id == tasks[0].task_id)
    ).scalar_one()
    assert row.queue_state == QueueState.DONE.value


# ---------- 触发点：规则分类报告回写 → batch_card ----------


def _seed_case_row(db: Session, *, session_id: str, round_id: str) -> None:
    db.add(
        SessionModel(
            session_id=session_id,
            product_id="ssd_9560",
            batch_id="B1",
            case_id=f"C-{session_id}",
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            stable_fingerprint=f"fp-{session_id}",
            env_topology={},
            git_context={},
            created_at=_NOW,
        )
    )
    db.flush()
    db.add(
        Round(
            round_id=round_id,
            session_id=session_id,
            round_no=1,
            trigger_user_id="zhangsan",
            queue_key="zhangsan",
            stage=Stage.CLEANING.value,
            round_status="RUNNING",
            started_at=_NOW,
        )
    )


def test_rule_report_completion_enqueues_batch_card(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    _seed_case_row(db_session, session_id="s1", round_id="r1")
    db_session.add(
        TaskQueue(
            task_id="T-W",
            batch_id="B1",
            session_id="s1",
            round_id="r1",
            round_no=1,
            product_id="ssd_9560",
            case_id="C-s1",
            task_type=TaskType.CASE_AI,
            queue_key="zhangsan",
            queue_state=QueueState.READY.value,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    db_session.flush()
    round_row = db_session.execute(select(Round).where(Round.round_id == "r1")).scalar_one()
    session_row = db_session.execute(
        select(SessionModel).where(SessionModel.session_id == "s1")
    ).scalar_one()
    task = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "T-W")).scalar_one()
    write_rule_report(
        db_session,
        task=task,
        round_row=round_row,
        session_row=session_row,
        now=_NOW,
        ids=_IdsSeq(),
        category="OTHER",
        detail="等待超时：依赖任务未在限期内返回结论，按规则分类转人工复核",
        referenced_logs=[],
        gap_note="等待超时",
    )
    db_session.commit()
    tasks = _notify_tasks(db_session)
    assert len(tasks) == 1
    payload = tasks[0].payload or {}
    assert payload["event"] == "batch_card"
    assert payload["category"] == "OTHER"
    poster = _Poster()
    worker = _worker(queue, poster)
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    assert "已出结论：1 例" in poster.texts[0]
    assert "案例 C-s1：OTHER" in poster.texts[0]


# ---------- 触发点：反馈级联否定 → 卡片追加行（去重） ----------


def _seed_citing(db: Session) -> Report:
    db.add(
        SessionModel(
            session_id="s-cite",
            product_id="ssd_9560",
            batch_id="B1",
            case_id="C-cite",
            domain_id="enterprise",
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            stable_fingerprint="fp-cite",
            env_topology={},
            git_context={},
            created_at=_NOW,
        )
    )
    db.flush()
    db.add(
        Round(
            round_id="r-cite",
            session_id="s-cite",
            round_no=1,
            trigger_user_id="zhangsan",
            queue_key="zhangsan",
            stage=Stage.COMPLETED.value,
            round_status="AWAITING_FEEDBACK",
            started_at=_NOW,
            finished_at=_NOW,
        )
    )
    db.flush()
    report = Report(
        report_id="rp-cite",
        round_id="r-cite",
        fault_info={},
        result={"root_cause": {"category": "OTHER", "detail": "引用同批案例结论"}},
        confidence_breakdown=[],
        evidence=[],
        referenced_logs=[],
        process_steps=[],
        citation={
            "source_session": "s-leader",
            "citing_status": "CITING",
            "source_round_no": "1",
        },
        gap_note=None,
        created_at=_NOW,
    )
    db.add(report)
    db.flush()
    return report


def test_negated_notify_dedup_and_post(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    report = _seed_citing(db_session)
    svc = KnowledgeService(db_session, ids=UuidIdGenerator(), clock=SystemClock())
    svc._enqueue_negated_notify(report, "s-leader", queue, _NOW)
    svc._enqueue_negated_notify(report, "s-leader", queue, _NOW)
    db_session.commit()
    tasks = _notify_tasks(db_session)
    assert len(tasks) == 1
    payload = tasks[0].payload or {}
    assert payload["event"] == "negated"
    assert "引用注记已标 CITING_STALE" in str(payload["message"])
    poster = _Poster()
    worker = _worker(queue, poster)
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    assert "结论被否定" in poster.cards[0]["markdown"]["title"]
    assert "引用注记已标 CITING_STALE" in poster.texts[0]


# ---------- 降级：any notify 失败不影响其他任务 ----------


def test_notify_failure_does_not_block_other_notify_tasks(db_session: Session) -> None:
    queue = QueueRepository(db_session)
    queue.enqueue(
        NewTask(
            task_id="T-1",
            batch_id="T-1",
            session_id=None,
            round_id="",
            round_no=0,
            product_id="ssd_9560",
            case_id="",
            task_type=TaskType.NOTIFY,
            queue_key="B1",
            max_attempts=1,
            payload={
                "event": "negated",
                "message": "第一批",
                "batch_id": "B1",
                "product_id": "ssd_9560",
            },
        ),
        _NOW,
    )
    queue.enqueue(
        NewTask(
            task_id="T-2",
            batch_id="T-2",
            session_id=None,
            round_id="",
            round_no=0,
            product_id="ssd_9560",
            case_id="",
            task_type=TaskType.NOTIFY,
            queue_key="B2",
            payload={
                "event": "negated",
                "message": "第二批",
                "batch_id": "B2",
                "product_id": "ssd_9560",
            },
        ),
        _NOW,
    )
    db_session.commit()
    failing_poster = _Poster()

    def poster(webhook: str, card: dict[str, Any]) -> None:
        if "第一批" in card["markdown"]["text"]:
            raise RuntimeError("webhook 不可用")
        failing_poster(webhook, card)

    worker = NotifyWorker(
        queue,
        config_fetcher=lambda product_id: NotifyConfig(webhook="https://d.example/hook"),
        poster=poster,
        clock=_Clock(_NOW),
    )
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 0
    assert worker.run_once(db_session, queue, _NOW, owner="w-1") == 1
    assert "第二批" in failing_poster.texts[0]
    row = db_session.execute(select(TaskQueue).where(TaskQueue.task_id == "T-1")).scalar_one()
    assert row.queue_state == QueueState.FAILED.value
