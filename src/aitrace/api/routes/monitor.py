# src/aitrace/api/routes/monitor.py
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from aitrace.api.deps import (
    get_audit,
    get_clock,
    get_metrics,
    get_session,
    get_settings,
    require_service_token,
)
from aitrace.api.schemas import MonitorSnapshotResponse
from aitrace.core.audit import AuditService
from aitrace.core.ids import SystemClock
from aitrace.core.settings import Settings
from aitrace.observability.metrics import MetricsRegistry
from aitrace.observability.monitor import build_monitor_snapshot

router = APIRouter(tags=["monitor"])


@router.get(
    "/aitrace/monitor",
    response_model=MonitorSnapshotResponse,
    dependencies=[Depends(require_service_token)],
)
def monitor_snapshot(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    metrics: Annotated[MetricsRegistry, Depends(get_metrics)],
    audit: Annotated[AuditService | None, Depends(get_audit)],
) -> MonitorSnapshotResponse:
    return MonitorSnapshotResponse(
        **build_monitor_snapshot(session, metrics, settings, now=clock.now(), audit=audit)
    )
