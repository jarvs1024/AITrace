# src/aitrace/api/routes/session_ops.py
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.api.deps import get_clock, get_ids, get_session, get_settings, require_service_token
from aitrace.api.schemas import (
    AttachmentUploadResponse,
    RediagnoseRequest,
    RediagnoseResponse,
)
from aitrace.core.enums import Stage, TaskType
from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings
from aitrace.persistence.models import Attachment, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask


def _scan_for_executable(data: bytes) -> str | None:
    """头部魔数扫描：附件仅作证据文本读取，拒绝可执行内容。"""
    head = data[:512]
    if head.startswith(b"\x7fELF"):
        return "ELF 可执行文件"
    if head.startswith(b"MZ"):
        return "PE/DOS 可执行文件"
    if head.lstrip().startswith(b"#!"):
        return "脚本解释器声明（shebang）"
    return None


router = APIRouter(tags=["session-ops"])

_TERMINAL_STAGES = {Stage.COMPLETED.value, Stage.FAILED.value}
_TERMINAL_STATUSES = {"AWAITING_FEEDBACK", "VALID", "INVALID"}


def _latest_round(session: Session, session_id: str) -> Round:
    row = (
        session.execute(
            select(Round).where(Round.session_id == session_id).order_by(Round.round_no.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话不存在或缺少轮次", http_status=404)
    return row


@router.post(
    "/aitrace/sessions/{session_id}/rediagnose",
    response_model=RediagnoseResponse,
    dependencies=[Depends(require_service_token)],
)
def rediagnose(
    session_id: str,
    body: RediagnoseRequest,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    ids: Annotated[UuidIdGenerator, Depends(get_ids)],
) -> RediagnoseResponse:
    record = session.get(SessionModel, session_id)
    if record is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话不存在", http_status=404)
    latest = _latest_round(session, session_id)
    terminal = latest.stage in _TERMINAL_STAGES or latest.round_status in _TERMINAL_STATUSES
    if not terminal:
        raise AitraceError(
            "SESSION_NOT_TERMINAL",
            f"会话最新轮次仍在进行，不可重诊：当前轮次 {latest.round_no}，"
            f"阶段 {latest.stage}，状态 {latest.round_status}；"
            "请等待诊断完成后重试",
            http_status=409,
        )
    now = clock.now()
    new_round_no = latest.round_no + 1
    prev_task = session.execute(
        select(TaskQueue)
        .where(
            TaskQueue.session_id == session_id,
            TaskQueue.round_no == latest.round_no,
            TaskQueue.task_type == TaskType.CASE_CLEAN.value,
        )
        .order_by(TaskQueue.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    base_payload = dict(prev_task.payload or {}) if prev_task is not None else {}
    attach_paths = [
        str(row.path)
        for row in session.execute(
            select(Attachment)
            .where(Attachment.session_id == session_id, Attachment.round_no == new_round_no)
            .order_by(Attachment.created_at.asc())
        ).scalars()
    ]
    payload = {
        **base_payload,
        "hint": body.hint,
        "attach_paths": attach_paths,
    }
    round_id = ids.new("R")
    task_id = ids.new("T")
    session.add(
        Round(
            round_id=round_id,
            session_id=session_id,
            round_no=new_round_no,
            trigger_user_id=body.trigger_user_id,
            queue_key=body.trigger_user_id,
            stage=Stage.QUEUED.value,
            round_status="RUNNING",
            started_at=now,
        )
    )
    QueueRepository(session).enqueue(
        NewTask(
            task_id=task_id,
            batch_id=record.batch_id,
            session_id=session_id,
            round_id=round_id,
            round_no=new_round_no,
            product_id=record.product_id,
            case_id=record.case_id,
            task_type=TaskType.CASE_CLEAN,
            queue_key=body.trigger_user_id,
            payload=payload,
        ),
        now,
    )
    record.latest_round_no = new_round_no
    record.status = "RUNNING"
    session.flush()
    return RediagnoseResponse(
        session_id=session_id, round_no=new_round_no, round_id=round_id, task_id=task_id
    )


@router.post(
    "/aitrace/sessions/{session_id}/attachments",
    response_model=AttachmentUploadResponse,
    dependencies=[Depends(require_service_token)],
)
async def upload_attachment(
    session_id: str,
    session: Annotated[Session, Depends(get_session)],
    clock: Annotated[SystemClock, Depends(get_clock)],
    ids: Annotated[UuidIdGenerator, Depends(get_ids)],
    settings: Annotated[Settings, Depends(get_settings)],
    uploaded_by: Annotated[str, Form(min_length=1)],
    file: Annotated[UploadFile, File()],
) -> AttachmentUploadResponse:
    record = session.get(SessionModel, session_id)
    if record is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话不存在", http_status=404)
    data = await file.read()
    max_bytes = settings.storage.max_attachment_bytes
    if len(data) > max_bytes:
        raise AitraceError(
            "ATTACHMENT_TOO_LARGE",
            f"附件大小超限：当前 {len(data)} 字节，上限 {max_bytes} 字节",
            http_status=413,
        )
    media_type = file.content_type or "application/octet-stream"
    allowed_types = settings.storage.attachment_allowed_media_types
    if media_type not in allowed_types:
        raise AitraceError(
            "ATTACHMENT_TYPE_FORBIDDEN",
            f"附件类型不允许：当前 {media_type}；允许清单：{'、'.join(allowed_types)}",
            http_status=415,
        )
    malicious = _scan_for_executable(data)
    if malicious is not None:
        raise AitraceError(
            "ATTACHMENT_MALICIOUS",
            f"附件内容疑似恶意文件（检测到 {malicious}）：附件只允许作为证据文本读取，"
            "禁止上传可执行内容",
            http_status=422,
        )
    original_name = Path(file.filename or "attachment").name
    attachment_id = ids.new("A")
    relative = f"rawlogs/{record.batch_id}/uploads/{attachment_id}_{original_name}"
    disk = settings.storage.data_dir / relative
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(data)
    now = clock.now()
    session.add(
        Attachment(
            attachment_id=attachment_id,
            session_id=session_id,
            round_no=record.latest_round_no + 1,
            media_type=media_type,
            path=relative,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            uploaded_by=uploaded_by,
            created_at=now,
        )
    )
    session.flush()
    return AttachmentUploadResponse(
        attachment_id=attachment_id,
        path=relative,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
