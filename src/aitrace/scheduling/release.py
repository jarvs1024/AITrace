# src/aitrace/scheduling/release.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.callback.enqueue import enqueue_result
from aitrace.core.enums import QueueState, TaskType
from aitrace.core.settings import AISettings
from aitrace.persistence.models import Batch, Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.aggregate import (
    build_citation,
    compute_batch_groups,
    passes_citation_threshold,
    promote_on_leader_failed,
)
from aitrace.scheduling.rule_report import write_rule_report


def sweep_waiting(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    now: datetime,
    settings: AISettings,
) -> int:
    waiting = (
        session.execute(
            select(TaskQueue)
            .where(TaskQueue.queue_state == QueueState.WAITING.value)
            .order_by(TaskQueue.updated_at.asc())
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    swept = 0
    for task in waiting:
        handled = _release_one(session, queue, ids, task, now, settings)
        if handled:
            swept += 1
    session.flush()
    return swept


def _release_one(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    task: TaskQueue,
    now: datetime,
    settings: AISettings,
) -> bool:
    if task.depends_on_task_id is None:
        if task.not_before is None or task.not_before <= now:
            return _promote(session, task, now)
        return False
    dep = session.get(TaskQueue, task.depends_on_task_id)
    if dep is None or dep.queue_state == QueueState.FAILED.value:
        return _promote_after_leader_failed(session, task, now)
    if dep.queue_state != QueueState.DONE.value:
        elapsed = (now - task.updated_at).total_seconds()
        if elapsed > settings.waiting_timeout_seconds:
            _rule_other(
                session,
                ids,
                task,
                now,
                "等待超时：依赖任务未在限期内返回结论，按规则分类转人工复核",
            )
            return True
        return False
    leader_round = (
        session.execute(select(Round).where(Round.session_id == dep.session_id)).scalars().first()
    )
    if leader_round is None or leader_round.round_status != "AWAITING_FEEDBACK":
        return _promote_after_leader_failed(session, task, now)
    category = None
    if leader_round.report_id:
        report = session.get(Report, leader_round.report_id)
        if report is not None:
            category = (report.result.get("root_cause") or {}).get("category")
    confidence = leader_round.confidence
    if passes_citation_threshold(confidence, category):
        return _cite(session, queue, ids, task, dep, leader_round, now)
    batch = session.execute(
        select(Batch).where(Batch.batch_id == task.batch_id)
    ).scalar_one_or_none()
    ai_used = int((batch.gate or {}).get("ai_used", 0)) if batch is not None else 0
    if ai_used < settings.batch_ai_budget:
        if batch is not None:
            batch.gate = {**(batch.gate or {}), "ai_used": ai_used + 1}
        return _promote(session, task, now)
    _rule_other(session, ids, task, now, "批内 AI 预算已耗尽且首例结论未达引用门槛，转人工复核")
    return True


def _promote(session: Session, task: TaskQueue, now: datetime) -> bool:
    task.queue_state = QueueState.READY.value
    task.task_type = TaskType.CASE_AI
    task.depends_on_task_id = None
    task.updated_at = now
    session_row = session.get(SessionModel, task.session_id)
    if session_row is not None:
        session_row.citation_group_id = task.session_id
    return True


def _promote_after_leader_failed(session: Session, task: TaskQueue, now: datetime) -> bool:
    session_row = session.get(SessionModel, task.session_id)
    if session_row is None:
        return False
    groups = (
        session.execute(
            select(SessionModel).where(
                SessionModel.batch_id == task.batch_id,
                SessionModel.stable_fingerprint == session_row.stable_fingerprint,
            )
        )
        .scalars()
        .all()
    )
    promote_on_leader_failed(session, compute_batch_groups(groups), now)
    return _promote(session, task, now)


def _cite(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    task: TaskQueue,
    dep: TaskQueue,
    leader_round: Round,
    now: datetime,
) -> bool:
    leader_report = session.get(Report, leader_round.report_id) if leader_round.report_id else None
    round_row = session.execute(select(Round).where(Round.round_id == task.round_id)).scalar_one()
    # dep 为 DONE 态分析任务（见 _release_one 守卫），会话归属由路由保证非空
    assert dep.session_id is not None
    citation = build_citation(
        dep.session_id, dep.round_no, leader_round.report_id or "", 1, "CITING", None
    )
    report = Report(
        report_id=ids.new("RP"),
        round_id=round_row.round_id,
        fault_info={"product": task.product_id, "symptom": "引用同批案例结论"},
        result=dict(leader_report.result)
        if leader_report is not None
        else {
            "root_cause": {"category": "OTHER", "detail": "引用源结论缺失", "evidence": []},
            "solution": None,
            "confidence": leader_round.confidence,
            "gap_note": None,
        },
        confidence_breakdown=list(leader_report.confidence_breakdown) if leader_report else [],
        evidence=list(leader_report.evidence) if leader_report else [],
        referenced_logs=list(leader_report.referenced_logs) if leader_report else [],
        process_steps=[
            {
                "step": "CITATION",
                "title": "聚合闸引用同批案例结论",
                "start": now.isoformat(),
                "end": now.isoformat(),
                "duration_ms": 0,
                "summary": f"引用会话 {dep.session_id} 第 {dep.round_no} 轮结论",
                "status": "SUCCESS",
            }
        ],
        citation=citation,
        gap_note=None,
        created_at=now,
    )
    session.add(report)
    round_row.report_id = report.report_id
    round_row.confidence = leader_round.confidence
    round_row.stage = "COMPLETED"
    round_row.round_status = "AWAITING_FEEDBACK"
    round_row.finished_at = now
    task.queue_state = QueueState.DONE.value
    task.updated_at = now
    session.flush()
    enqueue_result(
        session,
        queue,
        ids,
        task=task,
        report_payload={
            "root_cause": report.result["root_cause"],
            "solution": report.result.get("solution"),
            "confidence": leader_round.confidence,
            "gap_note": None,
        },
        citation=citation,
        now=now,
    )
    return True


def _rule_other(session: Session, ids: Any, task: TaskQueue, now: datetime, detail: str) -> None:
    round_row = session.execute(select(Round).where(Round.round_id == task.round_id)).scalar_one()
    session_row = session.get(SessionModel, task.session_id)
    if session_row is None:
        # 防御：task_queue 外键保证会话存在，此处兜底为空信息行，仅供报告读取拓扑信息
        session_row = SessionModel(
            session_id=task.session_id,
            product_id=task.product_id,
            batch_id=task.batch_id,
            case_id=task.case_id,
            domain_id="",
            profile_version="",
            trigger_user_id="",
            hardware_type="SSD",
            env_topology={},
            git_context={},
            created_at=now,
        )
    write_rule_report(
        session,
        task=task,
        round_row=round_row,
        session_row=session_row,
        now=now,
        ids=ids,
        category="OTHER",
        detail=detail,
        referenced_logs=[],
        gap_note=detail,
    )
