# src/aitrace/scheduling/router.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.callback.enqueue import enqueue_result
from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.settings import AISettings
from aitrace.persistence.models import Batch, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.aggregate import compute_batch_groups
from aitrace.scheduling.rule_report import write_rule_report
from aitrace.scheduling.types import NewTask


def route_ready_cleans(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    now: datetime,
    settings: AISettings,
) -> int:
    tasks = (
        session.execute(
            select(TaskQueue)
            .where(
                TaskQueue.task_type == TaskType.CASE_CLEAN,
                TaskQueue.queue_state == QueueState.READY.value,
            )
            .order_by(TaskQueue.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not tasks:
        return 0
    by_batch: dict[str, list[TaskQueue]] = {}
    for task in tasks:
        by_batch.setdefault(task.batch_id, []).append(task)
    routed = 0
    for batch_id, batch_tasks in by_batch.items():
        routed += _route_batch(session, queue, ids, batch_id, batch_tasks, now, settings)
    session.flush()
    return routed


def _route_batch(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    batch_id: str,
    tasks: list[TaskQueue],
    now: datetime,
    settings: AISettings,
) -> int:
    batch = session.execute(select(Batch).where(Batch.batch_id == batch_id)).scalar_one()
    gate = dict(batch.gate or {})
    ai_used = int(gate.get("ai_used", 0))
    failed_total = len(batch.failed_cases or []) or len(tasks)
    sessions = (
        session.execute(select(SessionModel).where(SessionModel.batch_id == batch_id))
        .scalars()
        .all()
    )
    groups = list(compute_batch_groups(sessions))
    escalated_group_keys: set[tuple[str, str]] = set()
    if not gate.get("escalated"):
        for group in groups:
            majority = settings.escalation_majority and len(group.members) > (failed_total + 1) // 2
            if len(group.members) >= settings.escalation_min_group or majority:
                escalated_group_keys.add((group.batch_id, group.stable_fp))
    if escalated_group_keys:
        gate["escalated"] = True
        common_task_id = create_batch_common(session, queue, ids, batch=batch, now=now)
        gate["batch_common_task_id"] = common_task_id
    task_by_session = {t.session_id: t for t in tasks}
    routed = 0
    for group in groups:
        key = (group.batch_id, group.stable_fp)
        if key in escalated_group_keys:
            common_task_id = gate["batch_common_task_id"]
            for member in group.members:
                task = task_by_session.get(member.session_id)
                if task is None:
                    continue
                task.queue_state = QueueState.WAITING.value
                task.depends_on_task_id = common_task_id
                task.updated_at = now
                member.citation_group_id = common_task_id
                routed += 1
            continue
        leader_task = task_by_session.get(group.leader.session_id)
        if leader_task is None:
            continue
        if ai_used < settings.batch_ai_budget:
            ai_used += 1
            leader_task.task_type = TaskType.CASE_AI
            leader_task.updated_at = now
            routed += 1
            for member in group.members[1:]:
                task = task_by_session.get(member.session_id)
                if task is None:
                    continue
                task.queue_state = QueueState.WAITING.value
                task.depends_on_task_id = leader_task.task_id
                task.updated_at = now
                member.citation_group_id = group.leader.session_id
                routed += 1
        else:
            detail = "批内 AI 预算已耗尽，转规则分类转人工复核"
            _rule_other(session, ids, leader_task, now, batch, detail)
            routed += 1
            for member in group.members[1:]:
                task = task_by_session.get(member.session_id)
                if task is None:
                    continue
                _rule_other(session, ids, task, now, batch, detail)
                routed += 1
    gate["ai_used"] = ai_used
    batch.gate = gate
    batch.updated_at = now
    return routed


def _rule_other(
    session: Session,
    ids: Any,
    task: TaskQueue,
    now: datetime,
    batch: Batch,
    detail: str,
) -> None:
    round_row = session.execute(select(Round).where(Round.round_id == task.round_id)).scalar_one()
    session_row = session.execute(
        select(SessionModel).where(SessionModel.session_id == task.session_id)
    ).scalar_one()
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
        gap_note="批内 AI 预算已耗尽（默认 3 次全量分析），本组未命中引用门槛，转人工复核",
    )
    enqueue_result(
        session,
        QueueRepository(session),
        ids,
        task=task,
        report_payload={
            "root_cause": {"category": "OTHER", "detail": detail, "evidence": []},
            "solution": None,
            "confidence": None,
            "gap_note": "批内 AI 预算已耗尽",
        },
        citation=None,
        now=now,
    )


def create_batch_common(
    session: Session,
    queue: QueueRepository,
    ids: Any,
    *,
    batch: Batch,
    now: datetime,
) -> str:
    gate = dict(batch.gate or {})
    existing = gate.get("batch_common_task_id")
    if existing:
        return str(existing)
    ref = session.execute(
        select(SessionModel)
        .where(SessionModel.batch_id == batch.batch_id, SessionModel.case_id != "")
        .order_by(SessionModel.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    domain_id = ref.domain_id if ref is not None else "enterprise"
    profile_version = ref.profile_version if ref is not None else "v1"
    hardware_type = ref.hardware_type if ref is not None else "SSD"
    session_id = ids.new("S")
    round_id = ids.new("R")
    task_id = ids.new("T")
    session.add(
        SessionModel(
            session_id=session_id,
            product_id=batch.product_id,
            batch_id=batch.batch_id,
            case_id="",
            domain_id=domain_id,
            profile_version=profile_version,
            trigger_user_id=batch.trigger_user_id,
            hardware_type=hardware_type,
            stable_fingerprint=None,
            env_topology={},
            git_context={},
            created_at=now,
        )
    )
    # Round 外键依赖 session 行，先落库保证插入顺序（仓库既有模式）
    session.flush()
    session.add(
        Round(
            round_id=round_id,
            session_id=session_id,
            round_no=1,
            trigger_user_id=batch.trigger_user_id,
            queue_key=batch.trigger_user_id,
            stage=Stage.QUEUED.value,
            round_status="RUNNING",
            started_at=now,
        )
    )
    queue.enqueue(
        NewTask(
            task_id=task_id,
            batch_id=batch.batch_id,
            session_id=session_id,
            round_id=round_id,
            round_no=1,
            product_id=batch.product_id,
            case_id="",
            task_type=TaskType.BATCH_COMMON,
            queue_key=batch.trigger_user_id,
            priority=2,
            payload={"batch_common": True},
        ),
        now,
    )
    common = session.execute(select(TaskQueue).where(TaskQueue.task_id == task_id)).scalar_one()
    common.queue_state = QueueState.READY.value
    gate["batch_common_task_id"] = task_id
    batch.gate = gate
    session.flush()
    return task_id
