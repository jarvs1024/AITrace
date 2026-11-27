from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aitrace.api.deps import (
    get_clock,
    get_ids,
    get_resolver,
    get_session,
    get_settings,
    require_service_token,
)
from aitrace.api.schemas import DeliveryPayload, DeliveryResponse
from aitrace.core.enums import QueueState
from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings
from aitrace.ingest.service import IngestService
from aitrace.persistence.models import TaskQueue
from aitrace.profiles.resolver import ProfileResolver
from aitrace.storage.watermark import WatermarkLevel, disk_fraction, evict, level_for

router = APIRouter(tags=["delivery"])

_BACKLOG_STATES = (QueueState.PENDING, QueueState.READY)
_REJECT_RETRY_AFTER = 60


def _backlog_count(session: Session, *, queue_key: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(TaskQueue)
        .where(TaskQueue.queue_state.in_(_BACKLOG_STATES))
    )
    if queue_key is not None:
        stmt = stmt.where(TaskQueue.queue_key == queue_key)
    return session.execute(stmt).scalar_one()


def _build_backpressure_checks(
    session: Session,
    settings: Settings,
    *,
    queue_key: str,
    clock: SystemClock,
) -> list[Callable[[], None]]:
    queue_cfg = settings.queue
    storage_cfg = settings.storage

    def global_depth_check() -> None:
        count = _backlog_count(session)
        if count >= queue_cfg.global_depth:
            raise AitraceError(
                "QUEUE_FULL",
                f"全局待处理任务数（当前 {count}）已达到上限（{queue_cfg.global_depth}），"
                "已启用背压暂拒新任务，请稍后重试",
                http_status=429,
                retryable=True,
                retry_after=_REJECT_RETRY_AFTER,
            )

    def trigger_depth_check() -> None:
        count = _backlog_count(session, queue_key=queue_key)
        if count >= queue_cfg.per_token_depth:
            raise AitraceError(
                "QUEUE_FULL",
                f"用户 {queue_key} 的待处理任务数（当前 {count}）已达到单令牌上限"
                f"（{queue_cfg.per_token_depth}），已启用背压暂拒新任务，请稍后重试",
                http_status=429,
                retryable=True,
                retry_after=_REJECT_RETRY_AFTER,
            )

    def watermark_check() -> None:
        data_dir = storage_cfg.data_dir
        l2_dir = data_dir / "rawlogs"
        fraction = disk_fraction(data_dir, l2_dir, storage_cfg.l2_quota_bytes)
        warn = storage_cfg.watermark_warn_fraction
        force = storage_cfg.watermark_force_fraction
        level = level_for(fraction, warn, force)
        if level is WatermarkLevel.OK:
            return
        evict(session, settings, force=level is WatermarkLevel.FORCE, now=clock.now())
        if level is WatermarkLevel.FORCE:
            fraction = disk_fraction(data_dir, l2_dir, storage_cfg.l2_quota_bytes)
            if level_for(fraction, warn, force) is WatermarkLevel.FORCE:
                raise AitraceError(
                    "DISK_FULL",
                    f"磁盘使用率 {fraction:.1%}（阈值 {force:.0%}），强制清理后仍超限，"
                    "已拒收新任务，请扩容或人工清理磁盘",
                    http_status=507,
                    retryable=True,
                    retry_after=_REJECT_RETRY_AFTER,
                )

    return [global_depth_check, trigger_depth_check, watermark_check]


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
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeliveryResponse:
    grayscale = settings.grayscale
    if grayscale.enabled and payload.product_id not in grayscale.product_ids:
        allowed = "、".join(grayscale.product_ids) or "<空>"
        raise AitraceError(
            "GRAYSCALE_BLOCKED",
            f"灰度发布阶段：当前产品线 {payload.product_id} 不在放行清单（{allowed}）内，"
            "该产品线尚未开启投递，请联系 AITrace 管理员加入灰度清单",
            http_status=403,
        )
    pre_checks = _build_backpressure_checks(
        session, settings, queue_key=payload.trigger_user_id, clock=clock
    )
    outcome = IngestService(session, resolver, clock, ids).deliver(payload, pre_checks=pre_checks)
    response.status_code = 202 if outcome.created_any else 200
    return outcome.response
