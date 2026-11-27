from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aitrace.persistence.base import EPOCH, Base


def _json_default() -> dict[str, Any]:
    return {}


class SessionModel(Base):
    __tablename__ = "session"
    __table_args__ = (
        Index("uq_session_delivery", "product_id", "batch_id", "case_id", unique=True),
    )

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hardware_type: Mapped[str] = mapped_column(String(16), nullable=False)
    stable_fingerprint: Mapped[str | None] = mapped_column(String(64))
    env_topology: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    git_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    latest_round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    citation_group_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Round(Base):
    __tablename__ = "round"
    __table_args__ = (Index("uq_round_no", "session_id", "round_no", unique=True),)

    round_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    exact_fingerprint: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    round_status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    report_id: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None]
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    __tablename__ = "report"

    report_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("round.round_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    fault_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence_breakdown: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    referenced_logs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    process_steps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    citation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gap_note: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Batch(Base):
    __tablename__ = "batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    failed_cases: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    passed_cases: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    batch_state: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    gate: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskQueue(Base):
    __tablename__ = "task_queue"
    __table_args__ = (
        Index("ix_task_dispatch", "queue_key", "queue_state", "priority", "created_at"),
        Index("ix_task_lease", "queue_state", "lease_until", "priority"),
        Index(
            "uq_task_case",
            "session_id",
            "round_no",
            "task_type",
            unique=True,
            postgresql_where=text("case_id <> '' AND task_type <> 'callback'"),
        ),
        Index(
            "uq_task_batch",
            "batch_id",
            "task_type",
            unique=True,
            postgresql_where=text("case_id = '' AND task_type <> 'callback'"),
        ),
    )

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=True
    )
    round_id: Mapped[str] = mapped_column(String(40), nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    queue_state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    dispatch_phase: Mapped[str | None] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    depends_on_task_id: Mapped[str | None] = mapped_column(String(40))
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=EPOCH, server_default=text("'epoch'")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=_json_default, server_default=text("'{}'::jsonb")
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserQueue(Base):
    __tablename__ = "user_queue"
    __table_args__ = (CheckConstraint("in_flight >= 0", name="ck_user_queue_in_flight"),)

    queue_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=EPOCH, server_default=text("'epoch'")
    )
    last_error: Mapped[str | None] = mapped_column(String(255))
    last_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseLibrary(Base):
    __tablename__ = "case_library"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stable_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_conclusion: Mapped[str] = mapped_column(String(4096), nullable=False)
    correction: Mapped[str | None] = mapped_column(String(4096))
    evidence_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    exact_fingerprints: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confirm_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    use_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_status: Mapped[str] = mapped_column(String(16), nullable=False, default="observing")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseContribution(Base):
    __tablename__ = "case_contribution"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stable_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    round_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    report_id: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_conclusion: Mapped[str] = mapped_column(String(4096), nullable=False)
    correction: Mapped[str | None] = mapped_column(String(4096))
    evidence_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    observation_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryHotFile(Base):
    __tablename__ = "memory_hot_file"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entries: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Profile(Base):
    __tablename__ = "profile"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Attachment(Base):
    __tablename__ = "attachment"

    attachment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=_json_default, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


TaskQueueRow = TaskQueue


class CallbackSeq(Base):
    __tablename__ = "callback_seq"

    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
