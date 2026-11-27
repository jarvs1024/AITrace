# src/aitrace/ai/worker.py
from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.ai.confidence import (
    AiStats,
    ConfidenceResult,
    ReferenceHit,
    compute_dims,
    score_confidence,
)
from aitrace.ai.prompts import (
    AnalysisContext,
    L1Artifacts,
    build_master_prompt,
    build_subagent_configs,
    hints_from_task_payload,
    load_l1_artifacts,
)
from aitrace.ai.qoder import QoderEvent, TokenExpiredError
from aitrace.ai.token_client import TokenFetchClient, TokenFetchError, TokenGrant
from aitrace.ai.validate import (
    ConclusionValidationError,
    ValidatedConclusion,
    extract_json,
    validate_conclusion,
)
from aitrace.callback.enqueue import enqueue_progress, enqueue_result
from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.settings import AISettings, AitestSettings
from aitrace.knowledge.prescreen import MemoryHit, prescreen_case_library
from aitrace.notify.enqueue import enqueue_case_notify
from aitrace.observability.metrics import MetricsRegistry
from aitrace.persistence.models import (
    Batch,
    Profile,
    Report,
    Round,
    SessionModel,
    TaskQueue,
)
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.models import DomainProfile
from aitrace.scheduling.pause import pause_queue


class _Terminal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _PauseAndRetry(Exception):
    def __init__(self, exc: TokenFetchError) -> None:
        super().__init__(exc.message)
        self.exc = exc


def _mark_round_failed(session: Session, task: TaskQueue, now: datetime) -> None:
    # R-M2-11C：终态失败（_Terminal 与兜底异常）时同步置 Round 为 FAILED，
    # 否则轮次将永远停留在 RUNNING（JIT 不可重试等路径不会走到 _persist）。
    round_row = session.execute(
        select(Round).where(Round.round_id == task.round_id)
    ).scalar_one_or_none()
    if round_row is not None:
        round_row.round_status = "FAILED"
        round_row.stage = Stage.FAILED.value
        round_row.finished_at = now
    session_row = session.get(SessionModel, task.session_id)
    if session_row is not None:
        session_row.status = "FAILED"


class AIWorker:
    def __init__(
        self,
        *,
        settings: AISettings,
        aitest: AitestSettings,
        adapter: Any,
        token_transport: httpx.BaseTransport | None,
        base_dir: Path,
        query_timeout: float,
        ids: Any,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._aitest = aitest
        self._adapter = adapter
        self._token_transport = token_transport
        self._base_dir = base_dir
        self._query_timeout = query_timeout
        self._ids = ids
        self._metrics = metrics

    def run_once(self, session: Session, now: datetime, owner: str) -> int:
        queue = QueueRepository(session, metrics=self._metrics)
        task = queue.claim_ai(owner, now)
        if task is None:
            return 0
        # 单次编排共享一个 id 生成器：同一 run_once 内可入队多条回调
        # （PROGRESS/RESULT/QUEUE_STATE），各自新建会重号撞 task_queue 主键
        # （T-1 重复）。运行时由应用装配注入真实 UuidIdGenerator。
        ids = self._ids
        runtime_dir = self._base_dir / "runtime" / task.task_id
        try:
            self._execute(session, queue, task, now, runtime_dir, ids)
        except _PauseAndRetry as exc:
            queue.retry(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                now,
                error={"code": exc.exc.code, "message": exc.exc.message},
            )
            pause_queue(
                session,
                queue,
                ids,
                queue_key=task.queue_key,
                now=now,
                code=exc.exc.code,
                message=exc.exc.message,
                settings=self._settings,
            )
        except _Terminal as exc:
            queue.finish(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                QueueState.FAILED,
                now,
                error={"code": exc.code, "message": exc.message},
            )
            _mark_round_failed(session, task, now)
        except Exception as exc:
            queue.finish(
                task.task_id,
                task.lease_owner or "",
                task.lease_generation,
                QueueState.FAILED,
                now,
                error={"code": "INTERNAL", "message": (str(exc) or type(exc).__name__)[:300]},
            )
            _mark_round_failed(session, task, now)
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            session.flush()
        return 1

    def _execute(
        self,
        session: Session,
        queue: QueueRepository,
        task: TaskQueue,
        now: datetime,
        runtime_dir: Path,
        ids: Any,
    ) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        grant = self._fetch_token(task)
        # claim_ai 只认分析任务行（CASE_AI/BATCH_COMMON），会话归属已由路由保证非空
        assert task.session_id is not None
        task.dispatch_phase = "AI_EXEC"
        session_row = session.get(SessionModel, task.session_id)
        if session_row is None:
            raise _Terminal("SESSION_MISSING", f"会话不存在：{task.session_id}")
        batch = session.get(Batch, task.batch_id)
        profile = self._load_profile(session, session_row.domain_id)
        self._progress(
            session,
            queue,
            ids,
            task,
            Stage.AI_CLASSIFYING,
            15,
            None,
            "Qoder 主引擎正在全局归类并定界（故障模式识别）",
            now,
        )
        ctx_session = session_row
        if task.task_type == TaskType.BATCH_COMMON:
            # 批次级分析没有自己的清洗回合：引用批内最早完成清洗的用例 L1 作为上下文
            member = self._batch_reference_member(session, task)
            if member is None:
                raise _Terminal(
                    "BATCH_REF_MISSING", "批次级分析缺少可引用的成员 L1 产物：批内无用例完成清洗"
                )
            artifacts = load_l1_artifacts(self._base_dir, member.session_id, member.latest_round_no)
            ctx_session = member
        else:
            artifacts = load_l1_artifacts(self._base_dir, task.session_id, task.round_no)
        hit = prescreen_case_library(
            session,
            domain_id=ctx_session.domain_id,
            stable_fingerprint=ctx_session.stable_fingerprint,
            round_id=task.round_id,
            now=now,
        )
        ctx = self._build_ctx(task, ctx_session, batch, artifacts, profile, hit)
        prompt = build_master_prompt(ctx)
        source_agent, probe_agent = build_subagent_configs(profile)
        events = self._stream(
            task,
            grant,
            prompt,
            subagents=[source_agent, probe_agent],
            runtime_dir=runtime_dir,
        )
        text_parts: list[str] = []
        ai_stats = AiStats()
        probe_marker: bool | None = None
        for event in events:
            if event.kind == "delta":
                text_parts.append(event.text)
            elif event.kind == "subagent":
                ai_stats = AiStats(
                    tool_calls=ai_stats.tool_calls + 1,
                    tool_errors=ai_stats.tool_errors,
                    source_review="DONE",
                )
                if "PROBE_FAIL" in event.text:
                    probe_marker = False
                elif "PROBE_OK" in event.text:
                    probe_marker = True
                if "ERROR" in event.text.upper():
                    ai_stats = AiStats(
                        tool_calls=ai_stats.tool_calls,
                        tool_errors=ai_stats.tool_errors + 1,
                        source_review=ai_stats.source_review,
                    )
            elif event.kind == "result":
                text_parts.append(event.text)
        if probe_marker is not None:
            ai_stats = AiStats(
                tool_calls=ai_stats.tool_calls,
                tool_errors=ai_stats.tool_errors,
                source_review=ai_stats.source_review,
                probe_success=probe_marker,
                probe_age_minutes=0.0,
            )
        full_text = "".join(text_parts)
        try:
            payload = extract_json(full_text)
            conclusion = validate_conclusion(
                payload,
                allowed_categories=profile.root_cause_enum,
                valid_hashes={entry["hash"] for entry in artifacts.referenced_logs},
            )
        except ConclusionValidationError as exc:
            raise _Terminal("VALIDATION_FAILED", str(exc)) from exc
        dims = compute_dims(
            metrics=artifacts.metrics,
            summary=artifacts.summary,
            ai_stats=ai_stats,
            reference=self._reference_from_hit(hit, batch),
            probing_done=probe_marker is not None,
            consistent=probe_marker,
        )
        caps = self._hard_caps(artifacts, conclusion.category)
        if probe_marker is False:
            caps["probe_conflict"] = True
        scored = score_confidence(
            dims,
            profile.confidence_weights,
            hard_caps=caps,
            low_threshold=self._settings.low_confidence_threshold,
        )
        self._persist(session, queue, task, session_row, conclusion, scored, artifacts, now, hit)
        queue.finish(
            task.task_id, task.lease_owner or "", task.lease_generation, QueueState.DONE, now
        )
        self._result_callback(session, queue, ids, task, conclusion, scored, now)

    def _fetch_token(self, task: TaskQueue) -> TokenGrant:
        client = TokenFetchClient(
            base_url=self._aitest.base_url,
            read_token=(
                self._aitest.read_token.get_secret_value() if self._aitest.read_token else ""
            ),
            timeout_seconds=self._aitest.timeout_seconds,
            transport=self._token_transport,
            registry=self._metrics,
        )
        try:
            return client.fetch(
                product_id=task.product_id,
                batch_id=task.batch_id,
                case_id=task.case_id,
                trigger_user_id=task.queue_key,
                action="analyze",
            )
        except TokenFetchError as exc:
            if not exc.retryable:
                raise _Terminal(exc.code, exc.message) from exc
            raise _PauseAndRetry(exc) from exc

    def _stream(
        self,
        task: TaskQueue,
        grant: TokenGrant,
        prompt: str,
        *,
        subagents: list[dict[str, Any]],
        runtime_dir: Path,
    ) -> Iterator[QoderEvent]:
        auth_expired = False

        def on_auth_expired() -> None:
            nonlocal auth_expired
            auth_expired = True

        async def consume() -> list[QoderEvent]:
            collected: list[QoderEvent] = []
            async for event in self._adapter.stream(
                grant.token,
                prompt,
                cwd=runtime_dir,
                on_auth_expired=on_auth_expired,
                subagents=subagents,
            ):
                collected.append(event)
            return collected

        try:
            return iter(asyncio.run(asyncio.wait_for(consume(), timeout=self._query_timeout)))
        except TimeoutError as exc:
            raise _Terminal(
                "AI_TIMEOUT", f"AI 查询超过硬超时 {self._query_timeout} 秒，任务失败"
            ) from exc
        except TokenExpiredError as exc:
            # R-M2-2：SDK 端认证失败（AssistantMessage.error=authentication_failed、
            # on_auth_expired 回调触发或 ResultMessage 错误文本）一律重排并暂停队列，
            # 重试时 JIT 重拉令牌——令牌在取票后失效与执行中失效走同一条恢复路径。
            message = (
                "执行期间令牌过期，任务重排并暂停队列"
                if auth_expired
                else f"SDK 报告令牌认证失败，任务重排并暂停队列：{exc}"
            )
            raise _PauseAndRetry(TokenFetchError("TOKEN_EXPIRED", message, retryable=True)) from exc

    def _load_profile(self, session: Session, domain_id: str) -> DomainProfile:
        row = session.get(Profile, domain_id)
        if row is None:
            raise _Terminal(
                "PROFILE_MISSING",
                f"领域画像快照缺失：{domain_id}；要求：先投递任务（投递时自动快照）或人工补齐画像",
            )
        return DomainProfile(**row.config)

    def _batch_reference_member(self, session: Session, task: TaskQueue) -> SessionModel | None:
        members = (
            session.execute(
                select(SessionModel)
                .where(SessionModel.batch_id == task.batch_id, SessionModel.case_id != "")
                .order_by(SessionModel.created_at.asc())
            )
            .scalars()
            .all()
        )
        for member in members:
            round_dir = (
                self._base_dir / "evidence" / member.session_id / f"round_{member.latest_round_no}"
            )
            if round_dir.is_dir():
                return member
        return None

    def _build_ctx(
        self,
        task: TaskQueue,
        session_row: SessionModel,
        batch: Batch | None,
        artifacts: L1Artifacts,
        profile: DomainProfile,
        hit: MemoryHit | None,
    ) -> AnalysisContext:
        batch_context = None
        if batch is not None:
            batch_context = {
                "failed_count": len(batch.failed_cases or []),
                "passed_count": len(batch.passed_cases or []),
                "escalated": bool((batch.gate or {}).get("escalated")),
            }
        memory_hits: tuple[dict[str, Any], ...] = ()
        if hit is not None:
            memory_hits = (
                {
                    "stable_fingerprint": session_row.stable_fingerprint,
                    "category": hit.category,
                    "correction": hit.correction,
                    "ai_conclusion": hit.conclusion,
                    "observing": hit.observing,
                },
            )
        return AnalysisContext(
            authoritative={
                "product_id": task.product_id,
                "batch_id": task.batch_id,
                "case_id": task.case_id,
                "hardware_type": session_row.hardware_type,
                "env_topology": session_row.env_topology,
                "git_context": session_row.git_context,
            },
            summary=artifacts.summary,
            slices=artifacts.slices,
            profile=profile,
            hints=hints_from_task_payload(task.payload or {}),
            batch_context=batch_context,
            memory_hits=memory_hits,
        )

    def _reference_from_hit(self, hit: MemoryHit | None, batch: Batch | None) -> ReferenceHit:
        if hit is not None:
            if hit.observing:
                return ReferenceHit(kind="observing")
            if hit.correction:
                return ReferenceHit(kind="conflict")
            if hit.confirm_count > 0:
                return ReferenceHit(kind="lesson")
            return ReferenceHit(kind="valid")
        if batch is not None and not (batch.passed_cases or []):
            return ReferenceHit(kind="none", no_control=True)
        return ReferenceHit(kind="none")

    def _hard_caps(self, artifacts: L1Artifacts, category: str) -> dict[str, bool]:
        caps: dict[str, bool] = {}
        availability = artifacts.summary.get("source_availability") or {}
        if (
            not availability
            or all(v == 0 for v in availability.values())
            or not artifacts.summary.get("anchor_utc")
        ):
            caps["no_anchor"] = True
        if category == "OTHER":
            caps["other_category"] = True
        return caps

    def _persist(
        self,
        session: Session,
        queue: QueueRepository,
        task: TaskQueue,
        session_row: SessionModel,
        conclusion: ValidatedConclusion,
        scored: ConfidenceResult,
        artifacts: L1Artifacts,
        now: datetime,
        hit: MemoryHit | None,
    ) -> None:
        round_row = session.execute(
            select(Round).where(Round.round_id == task.round_id)
        ).scalar_one()
        result: dict[str, Any] = {
            "root_cause": {
                "category": conclusion.category,
                "detail": conclusion.detail,
                "evidence": list(conclusion.evidence),
            },
            "solution": conclusion.solution,
            "confidence": scored.score,
            "gap_note": scored.gap_note,
        }
        if hit is not None:
            result["reference"] = {
                "stable_fingerprint": session_row.stable_fingerprint,
                "category": hit.category,
                "correction": hit.correction,
                "observing": hit.observing,
                "hits": hit.hits,
            }
        report = Report(
            report_id=self._ids.new("RP"),
            round_id=round_row.round_id,
            fault_info={
                "product": task.product_id,
                "symptom": conclusion.detail,
                "env_topology": {"host_ip": (session_row.env_topology or {}).get("host_ip", "-")},
            },
            result=result,
            confidence_breakdown=list(scored.breakdown),
            evidence=list(conclusion.evidence),
            referenced_logs=artifacts.referenced_logs,
            process_steps=[
                {
                    "step": "AI_EXEC",
                    "title": "Qoder 主引擎分析",
                    "start": now.isoformat(),
                    "end": now.isoformat(),
                    "duration_ms": 0,
                    "summary": f"结论分类 {conclusion.category}，置信度 {scored.score}",
                    "status": "SUCCESS",
                }
            ],
            citation={"citing_status": "NONE"},
            gap_note=scored.gap_note,
            created_at=now,
        )
        session.add(report)
        round_row.report_id = report.report_id
        round_row.confidence = scored.score
        round_row.stage = Stage.COMPLETED.value
        round_row.round_status = "AWAITING_FEEDBACK"
        round_row.finished_at = now
        session_row.status = "COMPLETED"
        session.flush()
        enqueue_case_notify(
            session,
            queue,
            self._ids,
            session_row=session_row,
            round_id=round_row.round_id,
            round_no=round_row.round_no,
            category=conclusion.category,
            detail=conclusion.detail,
            confidence=scored.score,
            now=now,
        )

    def _progress(
        self,
        session: Session,
        queue: QueueRepository,
        ids: Any,
        task: TaskQueue,
        stage: Stage,
        progress: int,
        waiting_reason: str | None,
        message: str,
        now: datetime,
    ) -> None:
        round_row = session.execute(
            select(Round).where(Round.round_id == task.round_id)
        ).scalar_one()
        round_row.stage = stage.value
        enqueue_progress(
            session,
            queue,
            ids,
            task=task,
            stage=stage,
            progress=progress,
            waiting_reason=waiting_reason,
            message=message,
            now=now,
        )

    def _result_callback(
        self,
        session: Session,
        queue: QueueRepository,
        ids: Any,
        task: TaskQueue,
        conclusion: ValidatedConclusion,
        scored: ConfidenceResult,
        now: datetime,
    ) -> None:
        enqueue_result(
            session,
            queue,
            ids,
            task=task,
            report_payload={
                "root_cause": {
                    "category": conclusion.category,
                    "detail": conclusion.detail,
                    "evidence": list(conclusion.evidence),
                },
                "solution": conclusion.solution,
                "confidence": scored.score,
                "gap_note": scored.gap_note,
            },
            citation={"citing_status": "NONE"},
            now=now,
        )
