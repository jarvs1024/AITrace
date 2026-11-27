from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState
from aitrace.persistence.models import Round, SessionModel, TaskQueue

_CITABLE_MIN_CONFIDENCE = 0.7


@dataclass(frozen=True)
class FingerprintGroup:
    batch_id: str
    stable_fp: str
    leader: SessionModel
    members: tuple[SessionModel, ...]


def compute_batch_groups(sessions: Iterable[SessionModel]) -> tuple[FingerprintGroup, ...]:
    by_key: dict[tuple[str, str], list[SessionModel]] = {}
    for s in sessions:
        if s.stable_fingerprint:
            by_key.setdefault((s.batch_id, s.stable_fingerprint), []).append(s)
    groups = []
    for (batch_id, fp), members in by_key.items():
        ordered = sorted(members, key=lambda m: m.created_at)
        groups.append(FingerprintGroup(batch_id, fp, ordered[0], tuple(ordered)))
    return tuple(groups)


def passes_citation_threshold(confidence: float | None, category: str | None) -> bool:
    return confidence is not None and confidence >= _CITABLE_MIN_CONFIDENCE and category != "OTHER"


def build_citation(
    source_session: str,
    source_round_no: int,
    source_report_id: str,
    cited_version: int,
    citing_status: str,
    cascaded_from: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source_session": source_session,
        "source_round_no": source_round_no,
        "source_report_id": source_report_id,
        "cited_version": cited_version,
        "citing_status": citing_status,
        "cascaded_from": cascaded_from,
    }


def cascade_rewrite(
    citations: dict[str, dict[str, Any]],
    *,
    new_source: tuple[str, int, str],
    cited_version: int,
    excluded: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    excluded = excluded or set()
    rewritten: dict[str, dict[str, Any]] = {}
    for session_id, citation in citations.items():
        if session_id in excluded:
            rewritten[session_id] = {**citation, "citing_status": "CITING_STALE"}
            continue
        previous = {
            "source_session": citation["source_session"],
            "source_round_no": citation["source_round_no"],
            "source_report_id": citation["source_report_id"],
        }
        rewritten[session_id] = {
            "source_session": new_source[0],
            "source_round_no": new_source[1],
            "source_report_id": new_source[2],
            "cited_version": cited_version,
            "citing_status": "CITING",
            "cascaded_from": previous,
        }
    return rewritten


def suspend_followers(session: Session, groups: Iterable[FingerprintGroup], now: datetime) -> int:
    suspended = 0
    for group in groups:
        leader_task = _clean_task(session, group.leader.session_id)
        if leader_task is None:
            continue
        for member in group.members[1:]:
            task = _clean_task(session, member.session_id)
            if task is None or task.queue_state != QueueState.PENDING.value:
                continue
            task.queue_state = QueueState.WAITING.value
            task.depends_on_task_id = leader_task.task_id
            task.updated_at = now
            member.citation_group_id = group.leader.session_id
            suspended += 1
    session.flush()
    return suspended


def promote_on_leader_failed(
    session: Session, groups: Iterable[FingerprintGroup], now: datetime
) -> int:
    promoted = 0
    for group in groups:
        leader_round = _latest_round(session, group.leader.session_id)
        if leader_round is None or leader_round.round_status != "FAILED":
            continue
        for member in group.members[1:]:
            task = _clean_task(session, member.session_id)
            if task is None or task.queue_state != QueueState.WAITING.value:
                continue
            task.queue_state = QueueState.READY.value
            task.depends_on_task_id = None
            task.updated_at = now
            member.citation_group_id = member.session_id
            promoted += 1
            break
    session.flush()
    return promoted


def _clean_task(session: Session, session_id: str) -> TaskQueue | None:
    return session.execute(
        select(TaskQueue)
        .where(TaskQueue.session_id == session_id, TaskQueue.task_type == "case_clean")
        .order_by(TaskQueue.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_round(session: Session, session_id: str) -> Round | None:
    return session.execute(
        select(Round).where(Round.session_id == session_id).order_by(Round.round_no.desc()).limit(1)
    ).scalar_one_or_none()
