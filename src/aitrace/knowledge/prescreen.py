# src/aitrace/knowledge/prescreen.py
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.persistence.models import CaseLibrary

# 同轮同指纹只计一次命中：AI 任务重试会以同一 round_id 重入，需防 use_hits 虚增。
# 进程内去重即可满足口径（use_hits 是软指标，看板口径本身用近似值）。
_SEEN: deque[str] = deque(maxlen=10_000)


@dataclass(frozen=True)
class MemoryHit:
    category: str
    correction: str | None
    conclusion: str
    observing: bool
    hits: int
    confirm_count: int


def prescreen_case_library(
    session: Session,
    *,
    domain_id: str,
    stable_fingerprint: str | None,
    round_id: str,
    now: datetime,
) -> MemoryHit | None:
    if not stable_fingerprint:
        return None
    key = f"{round_id}\u0000{stable_fingerprint}"
    recorded = key in _SEEN
    row = session.execute(
        select(CaseLibrary).where(
            CaseLibrary.domain_id == domain_id,
            CaseLibrary.stable_fingerprint == stable_fingerprint,
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    observing = row.entry_status == "observing" and row.observation_hits < 5
    if not recorded:
        row.use_hits += 1
        if observing:
            row.observation_hits += 1
        _SEEN.append(key)
        session.flush()
    return MemoryHit(
        category=row.category,
        correction=row.correction,
        conclusion=row.ai_conclusion,
        observing=observing,
        hits=row.use_hits,
        confirm_count=row.confirm_count,
    )
