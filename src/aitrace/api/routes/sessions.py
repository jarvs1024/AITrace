from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.api.deps import get_clock, get_session, require_service_token
from aitrace.api.schemas import SessionStatusResponse
from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue, UserQueue
from aitrace.scheduling.stage_view import build_stage_view

router = APIRouter(tags=["sessions"])


@router.get(
    "/aitrace/tasks/{session_id}",
    response_model=SessionStatusResponse,
    dependencies=[Depends(require_service_token)],
)
def get_session_status(
    session_id: str,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[SystemClock, Depends(get_clock)],
) -> SessionStatusResponse:
    record = session.get(SessionModel, session_id)
    if record is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话不存在", http_status=404)
    round_row = session.execute(
        select(Round).where(Round.session_id == session_id).order_by(Round.round_no.desc()).limit(1)
    ).scalar_one()
    task = session.execute(
        select(TaskQueue)
        .where(
            TaskQueue.session_id == session_id,
            TaskQueue.round_id == round_row.round_id,
            TaskQueue.task_type == "case_ai",
        )
        .order_by(TaskQueue.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if task is None:
        task = session.execute(
            select(TaskQueue)
            .where(
                TaskQueue.session_id == session_id,
                TaskQueue.round_id == round_row.round_id,
                TaskQueue.task_type == "case_clean",
            )
            .order_by(TaskQueue.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    queue = session.get(UserQueue, record.trigger_user_id)
    if task is None or queue is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话缺少排队任务", http_status=404)
    view = build_stage_view(task, queue, clock.now())
    report = session.execute(
        select(Report).where(Report.round_id == round_row.round_id)
    ).scalar_one_or_none()
    return SessionStatusResponse(
        session_id=record.session_id,
        batch_id=record.batch_id,
        case_id=record.case_id,
        product_id=record.product_id,
        hardware_type=record.hardware_type,
        domain_id=record.domain_id,
        profile_version=record.profile_version,
        round_no=round_row.round_no,
        stage=view.stage,
        progress=view.progress,
        waiting_reason=view.waiting_reason,
        message=view.message,
        report=None if report is None else {"report_id": report.report_id, "result": report.result},
        citation=None if report is None else report.citation,
    )
