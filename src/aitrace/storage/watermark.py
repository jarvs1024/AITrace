from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.settings import Settings
from aitrace.persistence.models import Batch, Round, SessionModel

_UNREVIEWED_ROUND_STATES = {"AWAITING_FEEDBACK", "FAILED", "ABANDONED"}
_INVALID_ROUND_STATES = {"INVALID"}
_VALID_ROUND_STATES = {"VALID"}

# 淘汰优先级（spec §5.3 三级水位 FORCE 档）：超TTL → 终态含未评价轮次批 →
# 无效轮次批 → 有效轮次批；WARN 档仅清理超 TTL 一类。
_PRIORITY_ORDER = ("ttl_expired", "unreviewed", "invalid", "valid")


class WatermarkLevel(Enum):
    OK = "OK"
    WARN = "WARN"
    FORCE = "FORCE"


@dataclass
class EvictionReport:
    by_reason: dict[str, int] = field(default_factory=dict)
    removed_dirs: int = 0
    freed_bytes: int = 0


def level_for(fraction: float, warn_fraction: float, force_fraction: float) -> WatermarkLevel:
    if fraction >= force_fraction:
        return WatermarkLevel.FORCE
    if fraction >= warn_fraction:
        return WatermarkLevel.WARN
    return WatermarkLevel.OK


def disk_fraction(data_dir: Path, l2_dir: Path, quota_bytes: int | None) -> float:
    if quota_bytes and quota_bytes > 0:
        used = _dir_size(l2_dir)
        return min(used / quota_bytes, 1.0)
    if not data_dir.exists():
        # 新部署首投前的空盘：无任何数据目录即视为 0 使用量
        return 0.0
    usage = shutil.disk_usage(data_dir)
    return usage.used / usage.total if usage.total else 1.0


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _round_class(
    round_status: str | None, finished_at: datetime | None, now: datetime, l1_ttl: timedelta
) -> str | None:
    if finished_at is not None and now - finished_at > l1_ttl:
        return "ttl_expired"
    if round_status in _UNREVIEWED_ROUND_STATES:
        return "unreviewed"
    if round_status in _INVALID_ROUND_STATES:
        return "invalid"
    if round_status in _VALID_ROUND_STATES:
        return "valid"
    return None


def _remove(candidates: dict[str, list[tuple[Path, Path | None]]], reason: str) -> tuple[int, int]:
    removed = 0
    freed = 0
    for path, parent in candidates[reason]:
        size = _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        if parent is not None:
            with contextlib.suppress(OSError):
                parent.rmdir()
        removed += 1
        freed += size
    return removed, freed


def evict(session: Session, settings: Settings, *, force: bool, now: datetime) -> EvictionReport:
    storage = settings.storage
    data_dir = storage.data_dir
    ev_report = EvictionReport()
    l1_ttl = timedelta(days=storage.l1_ttl_days)
    l2_ttl = timedelta(days=storage.l2_ttl_days)

    batches = {b.batch_id: b for b in session.execute(select(Batch)).scalars()}
    terminal_ids = {bid for bid, b in batches.items() if b.batch_state != "RUNNING"}

    candidates: dict[str, list[tuple[Path, Path | None]]] = {
        reason: [] for reason in _PRIORITY_ORDER
    }

    if terminal_ids:
        rows = session.execute(
            select(
                Round.round_id,
                SessionModel.session_id,
                Round.round_no,
                Round.round_status,
                Round.finished_at,
            )
            .join(SessionModel, SessionModel.session_id == Round.session_id)
            .where(SessionModel.batch_id.in_(terminal_ids))
        ).all()
        for _round_id, session_id, round_no, round_status, finished_at in rows:
            reason = _round_class(
                round_status,
                finished_at.astimezone() if finished_at else None,
                now,
                l1_ttl,
            )
            if reason is None:
                continue
            round_dir = data_dir / "evidence" / session_id / f"round_{round_no}"
            if round_dir.exists():
                candidates[reason].append((round_dir, round_dir.parent))

    rawlogs = data_dir / "rawlogs"
    if rawlogs.exists():
        for batch_dir in rawlogs.iterdir():
            if not batch_dir.is_dir():
                continue
            batch = batches.get(batch_dir.name)
            if batch is not None and batch.batch_id not in terminal_ids:
                continue
            mtime = datetime.fromtimestamp(batch_dir.stat().st_mtime, tz=UTC)
            if now - mtime > l2_ttl:
                candidates["ttl_expired"].append((batch_dir, None))

    reasons = _PRIORITY_ORDER if force else ("ttl_expired",)
    for reason in reasons:
        removed, freed = _remove(candidates, reason)
        if removed:
            ev_report.by_reason[reason] = ev_report.by_reason.get(reason, 0) + removed
            ev_report.removed_dirs += removed
            ev_report.freed_bytes += freed
    return ev_report
