# src/aitrace/callback/sequencer.py
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.persistence.models import CallbackSeq


def next_session_seq(session: Session, session_id: str, round_no: int) -> int:
    return _next(session, f"session:{session_id}:{round_no}")


def next_batch_seq(session: Session, batch_id: str) -> int:
    return _next(session, f"batch:{batch_id}")


def _next(session: Session, scope_key: str) -> int:
    row = session.execute(
        select(CallbackSeq).where(CallbackSeq.scope_key == scope_key).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        row = CallbackSeq(scope_key=scope_key, last_seq=1)
        session.add(row)
    else:
        row.last_seq += 1
    session.flush()
    return row.last_seq
