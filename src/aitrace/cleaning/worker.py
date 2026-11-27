from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.cleaning.artifacts import ArtifactWriter
from aitrace.cleaning.pipeline import PipelineOutcome, run_pipeline
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.core.enums import QueueState, Stage
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository


class CleaningWorker:
    def __init__(self, queue: QueueRepository, registry: CleaningRegistry, rules: RuleSet) -> None:
        self._queue = queue
        self._registry = registry
        self._rules = rules

    def run_once(
        self,
        session: Session,
        *,
        clock: SystemClock,
        ids: UuidIdGenerator,
        data_dir: Path,
        owner: str,
    ) -> int:
        task = self._queue.claim_clean(owner, clock.now())
        if task is None:
            return 0
        try:
            self.process_one(session, task, clock=clock, ids=ids, data_dir=data_dir)
        finally:
            session.flush()
        return 1

    def process_one(
        self,
        session: Session,
        task: TaskQueue,
        *,
        clock: SystemClock,
        ids: UuidIdGenerator,
        data_dir: Path,
    ) -> str:
        now = clock.now()
        session_row = session.get(SessionModel, task.session_id)
        if session_row is None:
            raise ValueError(f"会话不存在：{task.session_id}")
        # 守卫通过即说明 session_id 命中了真实会话行；清洗任务行由路由保证非空
        assert task.session_id is not None
        round_row = session.execute(
            select(Round).where(Round.round_id == task.round_id)
        ).scalar_one()
        started = now
        if round_row.stage == Stage.QUEUED.value:
            round_row.stage = Stage.CLEANING.value
        raw_root = data_dir / "rawlogs" / task.batch_id
        outcome = run_pipeline(
            payload=task.payload or {},
            authoritative={
                "product_id": task.product_id,
                "batch_id": task.batch_id,
                "case_id": task.case_id,
                "trigger_user_id": task.queue_key,
                "hardware_type": session_row.hardware_type,
                "env_topology": session_row.env_topology,
                "git_context": session_row.git_context,
            },
            raw_root=raw_root,
            profile=None,
            rules=self._rules,
            registry=self._registry,
        )
        duration_ms = int((clock.now() - started).total_seconds() * 1000)
        step = {
            "step": "CLEANING",
            "title": "多源日志脱水与切片",
            "start": started.isoformat(),
            "end": clock.now().isoformat(),
            "duration_ms": duration_ms,
            "summary": outcome.summary.get("status", ""),
            "status": "SUCCESS" if outcome.status != "failed" else "FAILED",
        }
        if outcome.status == "failed":
            return self._fail(session, task, round_row, outcome, now)
        session_row.stable_fingerprint = outcome.stable_fp
        round_row.exact_fingerprint = outcome.exact_fp
        writer = ArtifactWriter(data_dir)
        artifacts = writer.write(
            task.session_id,
            round_row.round_no,
            summary=outcome.summary,
            slices=outcome.slices,
            metrics=outcome.metrics,
            now=now,
        )
        if outcome.status == "rule_hit":
            report = Report(
                report_id=ids.new("RP"),
                round_id=round_row.round_id,
                fault_info={
                    "product": task.product_id,
                    "symptom": outcome.rule_hit.line if outcome.rule_hit else "",
                    "env_topology": {
                        "host_ip": (session_row.env_topology or {}).get("host_ip", "-")
                    },
                },
                result={
                    "root_cause": {
                        "category": (
                            outcome.rule_hit.category if outcome.rule_hit else "TEST_ENV_ERROR"
                        ),
                        "detail": outcome.rule_hit.detail if outcome.rule_hit else "",
                        "evidence": [],
                    },
                    "solution": None,
                    "confidence": None,
                    "gap_note": None,
                },
                confidence_breakdown=[],
                evidence=[],
                referenced_logs=artifacts.referenced_logs,
                process_steps=[step],
                citation={"citing_status": "NONE"},
                gap_note=None,
                created_at=now,
            )
            session.add(report)
            session.flush()
            round_row.report_id = report.report_id
            round_row.stage = Stage.COMPLETED.value
            round_row.round_status = "AWAITING_FEEDBACK"
            round_row.finished_at = now
            self._queue.finish(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                QueueState.DONE,
                now,
            )
            return "rule_hit"
        round_row.finished_at = now
        self._queue.finish(
            task.task_id,
            task.lease_owner or "",
            task.lease_generation,
            QueueState.READY,
            now,
        )
        return "ready"

    def _fail(
        self,
        session: Session,
        task: TaskQueue,
        round_row: Round,
        outcome: PipelineOutcome,
        now: datetime,
    ) -> str:
        if task.attempts == 0:
            self._queue.retry(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                now,
                error=outcome.error or {"code": "CLEAN_FAILED", "message": "清洗失败"},
            )
            return "failed"
        round_row.stage = Stage.FAILED.value
        round_row.round_status = "FAILED"
        round_row.finished_at = now
        self._queue.finish(
            task.task_id,
            task.lease_owner or "",
            task.lease_generation,
            QueueState.FAILED,
            now,
            error=outcome.error,
        )
        return "failed"
