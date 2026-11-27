# src/aitrace/callback/worker.py
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState
from aitrace.observability.metrics import MetricsRegistry
from aitrace.persistence.repositories.queue import QueueRepository


class CallbackWorker:
    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.Client],
        base_url: str,
        read_token: str,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._base_url = base_url.rstrip("/")
        self._read_token = read_token
        self._metrics = metrics

    def run_once(self, session: Session, now: datetime, owner: str) -> int:
        queue = QueueRepository(session, metrics=self._metrics)
        task = queue.claim_callback(owner, now)
        if task is None:
            return 0
        try:
            with self._client_factory() as client:
                response = client.post(
                    f"{self._base_url}/api/v1/aitest/aitrace/status",
                    json=task.payload,
                    headers={"X-Aitest-Read-Token": self._read_token},
                )
            if response.status_code == 200:
                task.callback_status = "SENT"
                queue.finish(
                    task.task_id,
                    task.lease_owner or "",
                    task.lease_generation,
                    QueueState.DONE,
                    now,
                )
                if self._metrics is not None:
                    self._metrics.window_counter("callback_ok").inc()
            else:
                raise httpx.HTTPStatusError(
                    f"回调被拒：HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
        except Exception as exc:
            message = (str(exc) or type(exc).__name__)[:300]
            if self._metrics is not None:
                self._metrics.counter("callback_fail_total").inc()
                self._metrics.window_counter("callback_fail").inc()
            result = queue.retry_callback(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                now,
                error={"code": "CALLBACK_FAILED", "message": message},
            )
            if result == QueueState.FAILED and self._metrics is not None:
                self._metrics.counter("callback_drop_total").inc()
        finally:
            session.flush()
        return 1
