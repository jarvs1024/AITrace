# src/aitrace/api/routes/dashboard.py
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aitrace.api.deps import get_clock, get_session, require_service_token
from aitrace.api.schemas import DashboardSummaryResponse
from aitrace.core.ids import SystemClock
from aitrace.persistence.models import (
    CaseLibrary,
    Report,
    Round,
    SessionModel,
    TaskQueue,
    UserQueue,
)

router = APIRouter(tags=["dashboard"])

_ROOT_CAUSE_CATEGORIES = ("TEST_ENV_ERROR", "FW_DEFECT", "SCRIPT_ISSUE", "HW_DEFECT", "OTHER")
_FEEDBACK_STATUSES = ("VALID", "INVALID", "AWAITING_FEEDBACK", "FAILED", "RUNNING")
_HISTOGRAM_BUCKETS = ("0.0-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0")
_QUEUE_STATES = ("PENDING", "CLEANING", "READY", "AI_RUNNING", "WAITING")


def _histogram_bucket(confidence: float) -> str:
    if confidence < 0.3:
        return "0.0-0.3"
    if confidence < 0.5:
        return "0.3-0.5"
    if confidence < 0.7:
        return "0.5-0.7"
    if confidence < 0.9:
        return "0.7-0.9"
    return "0.9-1.0"


def build_dashboard(session: Session, now: datetime, product_id: str | None) -> dict[str, Any]:
    sessions_q = select(SessionModel).where(SessionModel.case_id != "")
    if product_id:
        sessions_q = sessions_q.where(SessionModel.product_id == product_id)
    case_sessions = session.execute(sessions_q).scalars().all()

    by_member: dict[str, list[SessionModel]] = {}
    for row in case_sessions:
        by_member.setdefault(row.product_id, []).append(row)

    per_product_stats: dict[str, dict[str, int]] = {
        pid: {key: 0 for key in _FEEDBACK_STATUSES} for pid in by_member
    }
    feedback: dict[str, int] = {key: 0 for key in _FEEDBACK_STATUSES}
    root_cause: dict[str, int] = {key: 0 for key in _ROOT_CAUSE_CATEGORIES}
    histogram: dict[str, int] = {key: 0 for key in _HISTOGRAM_BUCKETS}
    confidences: list[float] = []
    round_distribution = {"1": 0, "2": 0, "3+": 0}
    round_total = 0
    latest_report_ids: list[str] = []

    if case_sessions:
        session_ids = [row.session_id for row in case_sessions]
        latest_rows = (
            session.execute(
                select(Round)
                .where(Round.session_id.in_(session_ids))
                .distinct(Round.session_id)
                .order_by(Round.session_id, Round.round_no.desc())
            )
            .scalars()
            .all()
        )
        latest_by_session = {row.session_id: row for row in latest_rows}
        for row in case_sessions:
            latest = latest_by_session[row.session_id]
            status = latest.round_status if latest.round_status in _FEEDBACK_STATUSES else "RUNNING"
            feedback[status] += 1
            per_product_stats[row.product_id][status] += 1
            if latest.confidence is not None:
                value = float(latest.confidence)
                confidences.append(value)
                histogram[_histogram_bucket(value)] += 1
            if latest.report_id:
                latest_report_ids.append(latest.report_id)
        for _sid, count in session.execute(
            select(Round.session_id, func.count())
            .where(Round.session_id.in_(session_ids))
            .group_by(Round.session_id)
        ).all():
            round_total += int(count)
            if count <= 1:
                round_distribution["1"] += 1
            elif count == 2:
                round_distribution["2"] += 1
            else:
                round_distribution["3+"] += 1
        for result in session.execute(
            select(Report.result).where(Report.report_id.in_(latest_report_ids))
        ).scalars():
            category = ((result or {}).get("root_cause") or {}).get("category")
            if category in _ROOT_CAUSE_CATEGORIES:
                root_cause[str(category)] += 1
            else:
                root_cause["OTHER"] += 1

    total = len(case_sessions)
    valid_total = feedback["VALID"]
    overview = {
        "session_count": total,
        "round_count": round_total,
        "valid_rate": valid_total / total if total else 0.0,
        "round_distribution": round_distribution,
        "feedback_distribution": feedback,
    }
    rounds_per_session: dict[str, int] = {}
    if case_sessions:
        for sid, count in session.execute(
            select(Round.session_id, func.count())
            .where(Round.session_id.in_(session_ids))
            .group_by(Round.session_id)
        ).all():
            rounds_per_session[str(sid)] = int(count)
    by_product: list[dict[str, Any]] = []
    for pid in sorted(by_member):
        rows = by_member[pid]
        stats = per_product_stats[pid]
        round_count = sum(rounds_per_session.get(row.session_id, 0) for row in rows)
        valid = stats["VALID"]
        by_product.append(
            {
                "product_id": pid,
                "session_count": len(rows),
                "round_count": round_count,
                "valid": valid,
                "invalid": stats["INVALID"],
                "awaiting": stats["AWAITING_FEEDBACK"],
                "failed": stats["FAILED"],
                "running": stats["RUNNING"],
                "valid_rate": valid / len(rows) if rows else 0.0,
            }
        )
    # 命中率近似口径：case_library.use_hits 总和 / 已完成轮次数（finished_at 非空）
    case_count = session.execute(select(func.count()).select_from(CaseLibrary)).scalar_one()
    use_hits = session.execute(
        select(func.coalesce(func.sum(CaseLibrary.use_hits), 0))
    ).scalar_one()
    finished_rounds = 0
    if case_sessions:
        finished_rounds = int(
            session.execute(
                select(func.count())
                .select_from(Round)
                .where(Round.session_id.in_(session_ids), Round.finished_at.is_not(None))
            ).scalar_one()
        )
    knowledge = {
        "case_count": int(case_count),
        "hot_memory_hit_rate": int(use_hits) / finished_rounds if finished_rounds else 0.0,
        "avg_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
        "low_confidence_ratio": (
            sum(1 for c in confidences if c < 0.5) / len(confidences) if confidences else 0.0
        ),
    }
    queue_counts = {key: 0 for key in _QUEUE_STATES}
    for state, count in session.execute(
        select(TaskQueue.queue_state, func.count())
        .where(TaskQueue.queue_state.in_(_QUEUE_STATES))
        .group_by(TaskQueue.queue_state)
    ).all():
        queue_counts[str(state)] = int(count)
    paused_users = int(
        session.execute(
            select(func.count()).select_from(UserQueue).where(UserQueue.state == "PAUSED_AUTH")
        ).scalar_one()
    )
    runtime = {"queue_counts": queue_counts, "paused_users": paused_users}
    return {
        "generated_at": now.isoformat(),
        "product_id": product_id,
        "overview": overview,
        "by_product": by_product,
        "root_cause_distribution": root_cause,
        "knowledge": knowledge,
        "runtime": runtime,
        "confidence_histogram": histogram,
    }


@router.get(
    "/aitrace/dashboard/summary",
    response_model=DashboardSummaryResponse,
    dependencies=[Depends(require_service_token)],
)
def dashboard_summary(
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    product_id: str | None = None,
) -> DashboardSummaryResponse:
    return DashboardSummaryResponse(**build_dashboard(session, clock.now(), product_id))
