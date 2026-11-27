from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from aitrace.api.deps import (
    get_clock,
    get_ids,
    get_resolver,
    get_session,
    require_service_token,
)
from aitrace.api.schemas import DeliveryPayload, DeliveryResponse
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.ingest.service import IngestService
from aitrace.profiles.resolver import ProfileResolver

router = APIRouter(tags=["delivery"])


@router.post(
    "/aitrace/tasks",
    response_model=DeliveryResponse,
    dependencies=[Depends(require_service_token)],
)
def create_tasks(
    payload: DeliveryPayload,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    resolver: Annotated[ProfileResolver, Depends(get_resolver)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    ids: Annotated[UuidIdGenerator, Depends(get_ids)],
) -> DeliveryResponse:
    outcome = IngestService(session, resolver, clock, ids).deliver(payload)
    response.status_code = 202 if outcome.created_any else 200
    return outcome.response
