# src/aitrace/notify/worker.py
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.core.ids import SystemClock
from aitrace.core.settings import AitestSettings
from aitrace.notify.cards import NotifyConfig, build_card
from aitrace.persistence.models import Batch, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_STATUS_ORDER = ("VALID", "INVALID", "AWAITING_FEEDBACK", "FAILED", "RUNNING")


class NotifyWorker:
    """钉钉通知消费者：claim_generic(notify) → 聚合卡片 → webhook 投递。

    降级原则：通知失败只影响本 notify 任务（退避重试至 max_attempts 后 FAILED），
    绝不抛出异常、绝不回调、绝不阻塞分析主链路。
    """

    def __init__(
        self,
        queue: QueueRepository | None,
        config_fetcher: Callable[[str], NotifyConfig],
        poster: Callable[[str, dict[str, Any]], None],
        *,
        cache_ttl: int = 300,
        default_webhook: str | None = None,
        clock: Any | None = None,
    ) -> None:
        self._queue = queue
        self._config_fetcher = config_fetcher
        self._poster = poster
        self._cache_ttl = cache_ttl
        self._default_webhook = default_webhook
        self._clock = clock if clock is not None else SystemClock()
        self._config_cache: dict[str, tuple[datetime, NotifyConfig]] = {}
        # 进程内按 batch_id 聚合：completed 计数 / 结论行 / 否定追加行
        self._agg: dict[str, dict[str, Any]] = {}

    def run_once(self, session: Session, queue: QueueRepository, now: datetime, owner: str) -> int:
        task = queue.claim_generic(owner, now, TaskType.NOTIFY)
        if task is None:
            return 0
        payload = dict(task.payload or {})
        event = str(payload.get("event") or "batch_card")
        batch_id = str(payload.get("batch_id") or task.queue_key)
        product_id = str(payload.get("product_id") or task.product_id or "")
        try:
            config = self._config_for(product_id)
            cards = self._cards_for(session, task, payload, event, batch_id, product_id, config)
            webhook = self._webhook_for(config, payload)
            for card in cards:
                self._poster(webhook, card)
            queue.finish(
                task.task_id, task.lease_owner or "", task.lease_generation, QueueState.DONE, now
            )
            return 1
        except Exception as exc:  # 降级：任何失败只伤本任务
            logger.warning("notify 任务失败 task_id=%s error=%s", task.task_id, exc)
            self._requeue_or_fail(session, task, now, exc)
            return 0

    def _config_for(self, product_id: str) -> NotifyConfig:
        now = self._clock.now()
        cached = self._config_cache.get(product_id)
        if cached is not None and (now - cached[0]).total_seconds() < self._cache_ttl:
            return cached[1]
        try:
            config = self._config_fetcher(product_id)
        except Exception:
            if self._default_webhook is None:
                raise
            config = NotifyConfig(webhook=self._default_webhook)
        self._config_cache[product_id] = (now, config)
        return config

    def _webhook_for(self, config: NotifyConfig, payload: dict[str, Any]) -> str:
        if config.high_risk_webhook and payload.get("high_risk"):
            return config.high_risk_webhook
        return config.webhook

    def _cards_for(
        self,
        session: Session,
        task: TaskQueue,
        payload: dict[str, Any],
        event: str,
        batch_id: str,
        product_id: str,
        config: NotifyConfig,
    ) -> list[dict[str, Any]]:
        agg = self._agg.setdefault(
            batch_id,
            {"product_id": product_id, "completed": 0, "conclusions": [], "negated": []},
        )
        cards: list[dict[str, Any]] = []
        detail_url = str(payload.get("detail_url") or "")
        if event == "batch_card":
            agg["completed"] += 1
            agg["conclusions"].append(
                {
                    "case_id": payload.get("case_id") or task.case_id or "-",
                    "category": payload.get("category", "OTHER"),
                    "confidence": payload.get("confidence"),
                }
            )
            agg["conclusions"] = agg["conclusions"][-20:]
            cards.append(
                build_card(
                    "batch_card",
                    batch_id=batch_id,
                    product_id=product_id,
                    counts={"completed": agg["completed"]},
                    conclusions=agg["conclusions"],
                    negated=agg["negated"],
                    detail_url=detail_url,
                    at_users=config.at_users,
                )
            )
            high_risk = payload.get("high_risk")
            if isinstance(high_risk, dict):
                cards.append(
                    build_card(
                        "high_risk",
                        batch_id=batch_id,
                        product_id=product_id,
                        case_id=str(payload.get("case_id") or task.case_id or "-"),
                        keywords=list(high_risk.get("keywords") or []),
                        detail=str(high_risk.get("detail") or ""),
                        detail_url=detail_url,
                        at_users=config.at_users,
                    )
                )
        elif event == "negated":
            message = str(payload.get("message") or "")
            if message:
                agg["negated"].append(message)
                agg["negated"] = agg["negated"][-20:]
            cards.append(
                build_card(
                    "negated",
                    batch_id=batch_id,
                    product_id=product_id,
                    negated=agg["negated"] or [message],
                    detail_url=detail_url,
                    at_users=config.at_users,
                )
            )
        elif event == "final":
            counts, batch_state = self._final_counts(session, batch_id)
            cards.append(
                build_card(
                    "final",
                    batch_id=batch_id,
                    product_id=product_id,
                    counts=counts,
                    conclusions=agg["conclusions"],
                    negated=agg["negated"],
                    detail_url=detail_url,
                    at_users=config.at_users,
                    batch_state=batch_state,
                )
            )
            self._agg.pop(batch_id, None)
        return cards

    def _final_counts(self, session: Session, batch_id: str) -> tuple[dict[str, int], str]:
        rounds = (
            session.execute(
                select(Round)
                .join(SessionModel, Round.session_id == SessionModel.session_id)
                .where(SessionModel.batch_id == batch_id)
            )
            .scalars()
            .all()
        )
        counts = {status: 0 for status in _STATUS_ORDER}
        for row in rounds:
            status = row.round_status if row.round_status in _STATUS_ORDER else "RUNNING"
            counts[status] += 1
        batch = session.execute(
            select(Batch).where(Batch.batch_id == batch_id)
        ).scalar_one_or_none()
        return counts, batch.batch_state if batch is not None else ""

    def _requeue_or_fail(
        self, session: Session, task: TaskQueue, now: datetime, exc: Exception
    ) -> None:
        row = session.get(TaskQueue, task.task_id)
        if row is None:
            return
        row.attempts += 1
        row.error = {"code": "NOTIFY_DELIVERY_FAILED", "message": str(exc)[:300]}
        row.lease_owner = None
        row.lease_until = _EPOCH
        row.dispatch_phase = None
        row.updated_at = now
        if row.attempts >= row.max_attempts:
            row.queue_state = QueueState.FAILED.value
            row.next_retry_at = None
        else:
            row.queue_state = QueueState.PENDING.value
            row.next_retry_at = now + timedelta(seconds=min(30 * (2 ** (row.attempts - 1)), 1800))
        session.flush()


def build_notify_config_fetcher(settings: AitestSettings) -> Callable[[str], NotifyConfig]:
    base = settings.base_url.rstrip("/")
    read_token = settings.read_token.get_secret_value() if settings.read_token else ""
    timeout = settings.timeout_seconds

    def fetch(product_id: str) -> NotifyConfig:
        headers = {"X-Aitest-Read-Token": read_token} if read_token else {}
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(
                f"{base}/api/v1/aitest/notify-config",
                params={"product_id": product_id},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            return NotifyConfig(
                webhook=str(data["webhook"]),
                at_users=list(data.get("at_users") or []),
                high_risk_webhook=data.get("high_risk_webhook"),
            )

    return fetch


def build_notify_poster() -> Callable[[str, dict[str, Any]], None]:
    def post(webhook: str, card: dict[str, Any]) -> None:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.post(webhook, json=card)
            response.raise_for_status()

    return post
