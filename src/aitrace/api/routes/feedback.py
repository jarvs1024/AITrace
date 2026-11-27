from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aitrace.api.deps import get_clock, get_ids, get_session, require_service_token
from aitrace.api.schemas import FeedbackRequest, FeedbackResponse
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.knowledge.service import KnowledgeService
from aitrace.persistence.repositories.queue import QueueRepository

router = APIRouter(tags=["feedback"])


@router.post(
    "/aitrace/feedback",
    response_model=FeedbackResponse,
    dependencies=[Depends(require_service_token)],
)
def submit_feedback(
    body: FeedbackRequest,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    ids: Annotated[UuidIdGenerator, Depends(get_ids)],
) -> FeedbackResponse:
    outcome = KnowledgeService(session, ids=ids, clock=clock).apply_feedback(
        session_id=body.session_id,
        batch_id=body.batch_id,
        case_id=body.case_id,
        reviewer=body.reviewer,
        action=body.action,
        correction=body.correction.model_dump() if body.correction else None,
        queue=QueueRepository(session),
        round_no=body.round_no,
    )
    return FeedbackResponse(
        feedback_id=outcome.feedback_id,
        session_id=body.session_id,
        round_no=outcome.round_no,
        version=outcome.version,
        status="applied" if outcome.applied else "idempotent",
    )
