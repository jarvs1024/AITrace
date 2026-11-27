# src/aitrace/knowledge/service.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.persistence.models import (
    CaseContribution,
    CaseLibrary,
    Report,
    Round,
    SessionModel,
    TaskQueue,
)
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

_OBSERVATION_LIMIT = 5
_EVIDENCE_MAX = 4096
_FEEDBACKABLE_ROUND_STATUSES = ("AWAITING_FEEDBACK", "VALID", "INVALID")


@dataclass(frozen=True)
class FeedbackOutcome:
    feedback_id: str
    round_no: int
    version: int
    applied: bool
    cascaded: int


def _correction_key(round_no: int, correction: dict[str, Any] | None) -> str:
    return json.dumps(correction, ensure_ascii=False, sort_keys=True) if correction else ""


def _evidence_summary(report: Report | None) -> str:
    if report is None:
        return ""
    excerpts = [str(item.get("excerpt") or "") for item in (report.evidence or [])]
    text = "；".join(e for e in excerpts if e)
    if not text:
        root_cause = report.result.get("root_cause") or {}
        text = str(root_cause.get("detail") or "")
    return text[:_EVIDENCE_MAX]


class KnowledgeService:
    """两态反馈驱动的知识进化闭环：贡献记账、投影重算、撤回、聚合闸引用级联。"""

    def __init__(self, session: Session, *, ids: UuidIdGenerator, clock: SystemClock) -> None:
        self._session = session
        self._ids = ids
        self._clock = clock

    def apply_feedback(
        self,
        *,
        session_id: str,
        batch_id: str,
        case_id: str,
        reviewer: str,
        action: Literal["VALID", "INVALID"],
        correction: dict[str, Any] | None,
        queue: QueueRepository,
        round_no: int | None = None,
    ) -> FeedbackOutcome:
        session_row = self._session.get(SessionModel, session_id)
        if session_row is None:
            raise AitraceError(
                "DELIVERY_NOT_FOUND",
                f"会话不存在：当前 session_id {session_id}",
                http_status=404,
            )
        if session_row.batch_id != batch_id or session_row.case_id != case_id:
            raise AitraceError(
                "FEEDBACK_CONFLICT",
                "反馈定位与会话不一致："
                f"批次 ID 当前 {session_row.batch_id}，请求值 {batch_id}；"
                f"用例 ID 当前 {session_row.case_id!r}，请求值 {case_id!r}",
                http_status=400,
            )
        target_round_no = round_no or session_row.latest_round_no
        round_row = self._session.execute(
            select(Round).where(Round.session_id == session_id, Round.round_no == target_round_no)
        ).scalar_one_or_none()
        if round_row is None:
            raise AitraceError(
                "ROUND_NOT_FOUND",
                f"轮次不存在：session_id {session_id}，round_no {target_round_no}",
                http_status=404,
            )
        if round_row.round_status not in _FEEDBACKABLE_ROUND_STATUSES:
            raise AitraceError(
                "FEEDBACK_ROUND_NOT_FINAL",
                "轮次未完成，不可评价："
                f"当前轮次状态 {round_row.round_status}，"
                f"允许值：{' / '.join(_FEEDBACKABLE_ROUND_STATUSES)}",
                http_status=409,
            )
        if action == "VALID" and not round_row.report_id:
            raise AitraceError(
                "FEEDBACK_NO_REPORT",
                f"轮次缺少诊断报告，不可评价 VALID：session_id {session_id}，"
                f"round_no {target_round_no}",
                http_status=400,
            )
        report = self._session.get(Report, round_row.report_id) if round_row.report_id else None
        previous = round_row.feedback or {}
        content_key = _correction_key(target_round_no, correction)
        # 幂等：同一评审人、同一动作、同一修正内容重复提交，返回既有结果
        if (
            previous
            and previous.get("reviewer") == reviewer
            and previous.get("action") == action
            and previous.get("correction_key") == content_key
        ):
            return FeedbackOutcome(
                feedback_id=str(previous["feedback_id"]),
                round_no=target_round_no,
                version=int(previous["version"]),
                applied=False,
                cascaded=0,
            )
        now = self._clock.now()
        version = int(previous.get("version") or 0) + 1
        feedback_id = self._ids.new("F")
        knowledge_touched = self._withdraw_previous(previous, session_id, target_round_no, now)
        stable_fp = session_row.stable_fingerprint
        writable = bool(stable_fp) and session_row.case_id != ""
        if writable and stable_fp is not None:
            if action == "VALID" and report is not None:
                root_cause = report.result.get("root_cause") or {}
                self._session.add(
                    CaseContribution(
                        domain_id=session_row.domain_id,
                        stable_fingerprint=stable_fp,
                        session_id=session_id,
                        round_no=target_round_no,
                        feedback_version=version,
                        kind="valid",
                        status="active",
                        report_id=round_row.report_id or "",
                        category=str(root_cause.get("category") or "OTHER"),
                        ai_conclusion=str(root_cause.get("detail") or "")[:_EVIDENCE_MAX],
                        correction=None,
                        evidence_summary=_evidence_summary(report),
                        observation_hits=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                knowledge_touched = True
            elif action == "INVALID" and correction is not None:
                root_cause = (report.result.get("root_cause") or {}) if report else {}
                self._session.add(
                    CaseContribution(
                        domain_id=session_row.domain_id,
                        stable_fingerprint=stable_fp,
                        session_id=session_id,
                        round_no=target_round_no,
                        feedback_version=version,
                        kind="lesson",
                        status="active",
                        report_id=round_row.report_id or "",
                        category=str(correction["root_cause_category"]),
                        ai_conclusion=str(root_cause.get("detail") or "")[:_EVIDENCE_MAX],
                        correction=str(correction["reason"])[:_EVIDENCE_MAX],
                        evidence_summary=_evidence_summary(report),
                        observation_hits=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                knowledge_touched = True
            if knowledge_touched:
                self._upsert_library(
                    session_row.domain_id, stable_fp, round_row.exact_fingerprint, now
                )
        # 聚合闸引用级联（INVALID 否定源 / VALID 恢复直引），只改引用注记不重跑 AI
        cascaded = self._cascade(action, session_id, target_round_no, queue, now)
        round_row.feedback = {
            "feedback_id": feedback_id,
            "reviewer": reviewer,
            "action": action,
            "version": version,
            "correction_key": content_key,
            "correction": correction,
            "applied_at": now.isoformat(),
            "cascaded": cascaded,
        }
        round_row.round_status = action
        if knowledge_touched:
            self._enqueue_materialize(queue, session_row.domain_id, now)
        self._session.flush()
        return FeedbackOutcome(
            feedback_id=feedback_id,
            round_no=target_round_no,
            version=version,
            applied=True,
            cascaded=cascaded,
        )

    def _withdraw_previous(
        self, previous: dict[str, Any], session_id: str, round_no: int, now: datetime
    ) -> bool:
        if not previous.get("version"):
            return False
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(CaseContribution)
                .where(
                    CaseContribution.session_id == session_id,
                    CaseContribution.round_no == round_no,
                    CaseContribution.feedback_version == int(previous["version"]),
                    CaseContribution.status == "active",
                )
                .values(status="withdrawn", updated_at=now)
            ),
        )
        return result.rowcount > 0

    def _upsert_library(
        self, domain_id: str, stable_fp: str, exact_fp: str | None, now: datetime
    ) -> None:
        row = self._session.execute(
            select(CaseLibrary)
            .where(CaseLibrary.domain_id == domain_id, CaseLibrary.stable_fingerprint == stable_fp)
            .with_for_update()
        ).scalar_one_or_none()
        active = (
            self._session.execute(
                select(CaseContribution)
                .where(
                    CaseContribution.domain_id == domain_id,
                    CaseContribution.stable_fingerprint == stable_fp,
                    CaseContribution.status == "active",
                )
                .order_by(CaseContribution.created_at.desc())
            )
            .scalars()
            .all()
        )
        lessons = [c for c in active if c.kind == "lesson"]
        valids = [c for c in active if c.kind == "valid"]
        newest_lesson = lessons[0] if lessons else None
        newest_valid = valids[0] if valids else None
        if newest_lesson is not None:
            category = newest_lesson.category
            ai_conclusion = newest_lesson.ai_conclusion
            correction = newest_lesson.correction
            evidence_summary = newest_lesson.evidence_summary
            observation_hits = newest_lesson.observation_hits
            entry_status = "observing" if observation_hits < _OBSERVATION_LIMIT else "lesson"
        elif newest_valid is not None:
            category = newest_valid.category
            ai_conclusion = newest_valid.ai_conclusion
            correction = newest_valid.correction
            evidence_summary = newest_valid.evidence_summary
            observation_hits = 0
            entry_status = "valid"
        else:
            category = row.category if row is not None else "OTHER"
            ai_conclusion = row.ai_conclusion if row is not None else ""
            correction = None
            evidence_summary = ""
            observation_hits = 0
            entry_status = "demoted"
        exact_fps = list(row.exact_fingerprints or []) if row is not None else []
        if exact_fp and exact_fp not in exact_fps:
            exact_fps.append(exact_fp)
        if row is not None:
            row.category = category
            row.ai_conclusion = ai_conclusion
            row.correction = correction
            row.evidence_summary = evidence_summary
            row.confirm_count = len(active)
            row.observation_hits = observation_hits
            row.entry_status = entry_status
            row.exact_fingerprints = exact_fps
            row.version += 1
            row.updated_at = now
        else:
            self._session.add(
                CaseLibrary(
                    domain_id=domain_id,
                    stable_fingerprint=stable_fp,
                    category=category,
                    ai_conclusion=ai_conclusion,
                    correction=correction,
                    evidence_summary=evidence_summary,
                    exact_fingerprints=exact_fps,
                    confirm_count=len(active),
                    use_hits=0,
                    observation_hits=observation_hits,
                    entry_status=entry_status,
                    version=1,
                    updated_at=now,
                )
            )

    def _cascade(
        self,
        action: Literal["VALID", "INVALID"],
        session_id: str,
        round_no: int,
        queue: QueueRepository,
        now: datetime,
    ) -> int:
        if action == "INVALID":
            citing = (
                self._session.execute(
                    select(Report).where(
                        Report.citation["source_session"].astext == session_id,
                        Report.citation["source_round_no"].astext == str(round_no),
                        Report.citation["citing_status"].astext == "CITING",
                    )
                )
                .scalars()
                .all()
            )
            cascaded = 0
            for report in citing:
                citation = report.citation or {}
                previous = {
                    key: citation.get(key)
                    for key in ("source_session", "source_round_no", "source_report_id")
                }
                report.citation = {
                    **citation,
                    "citing_status": "CITING_STALE",
                    "cascaded_from": previous,
                }
                cascaded += 1
                self._enqueue_negated_notify(report, session_id, queue, now)
            return cascaded
        citing = (
            self._session.execute(
                select(Report).where(
                    Report.citation["source_session"].astext == session_id,
                    Report.citation["source_round_no"].astext == str(round_no),
                    Report.citation["citing_status"].astext == "CITING_STALE",
                )
            )
            .scalars()
            .all()
        )
        for report in citing:
            report.citation = {**(report.citation or {}), "citing_status": "CITING"}
        return 0

    def _enqueue_negated_notify(
        self, report: Report, source_session_id: str, queue: QueueRepository, now: datetime
    ) -> None:
        round_row = self._session.execute(
            select(Round).where(Round.round_id == report.round_id)
        ).scalar_one_or_none()
        if round_row is None:
            return
        session_row = self._session.get(SessionModel, round_row.session_id)
        if session_row is None:
            return
        existing = self._session.execute(
            select(TaskQueue).where(
                TaskQueue.session_id == round_row.session_id,
                TaskQueue.round_no == round_row.round_no,
                TaskQueue.task_type == TaskType.NOTIFY,
            )
        ).first()
        if existing is not None:
            return
        queue.enqueue(
            NewTask(
                task_id=self._ids.new("T"),
                batch_id=session_row.batch_id,
                session_id=round_row.session_id,
                round_id=round_row.round_id,
                round_no=round_row.round_no,
                product_id=session_row.product_id,
                case_id=session_row.case_id,
                task_type=TaskType.NOTIFY,
                queue_key=session_row.batch_id,
                payload={
                    "event": "negated",
                    "batch_id": session_row.batch_id,
                    "session_id": round_row.session_id,
                    "source_session_id": source_session_id,
                    "message": "同批引用的首例结论已被否定，本案例引用注记已标 CITING_STALE",
                },
            ),
            now,
        )

    def _enqueue_materialize(self, queue: QueueRepository, domain_id: str, now: datetime) -> None:
        existing = self._session.execute(
            select(TaskQueue).where(
                TaskQueue.task_type == TaskType.MEMORY_MATERIALIZE,
                TaskQueue.queue_key == domain_id,
                TaskQueue.queue_state == QueueState.PENDING,
            )
        ).first()
        if existing is not None:
            return
        # batch_id 需唯一：历史 DONE 行若复用固定值会撞 uq_task_batch 部分唯一索引
        task_id = self._ids.new("T")
        queue.enqueue(
            NewTask(
                task_id=task_id,
                batch_id=task_id,
                session_id=None,
                round_id="",
                round_no=0,
                product_id="",
                case_id="",
                task_type=TaskType.MEMORY_MATERIALIZE,
                queue_key=domain_id,
                payload={"domain_id": domain_id},
            ),
            now,
        )
