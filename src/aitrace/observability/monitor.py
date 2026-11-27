"""监控快照：看板⑥实时监控台与告警巡检共用的纯函数数据源。

只输出计数、耗时、比例与状态，不携带任何令牌或敏感凭据值。
等待时长口径：now - task.updated_at（调度器是任务状态唯一写入者）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.audit import AuditService
from aitrace.core.settings import Settings
from aitrace.observability.metrics import MetricsRegistry
from aitrace.persistence.models import TaskQueue, UserQueue
from aitrace.storage.watermark import disk_fraction, level_for

_QUEUE_STATES = ("PENDING", "CLEANING", "READY", "AI_RUNNING", "WAITING")
_ACTIVE_STATES = ("PENDING", "CLEANING", "READY", "AI_RUNNING", "WAITING")
_WAIT_BUCKETS = ("bucket_0", "bucket_1_2", "bucket_3_4", "bucket_gt4")


def _elapsed(now: datetime, at: datetime | None) -> float:
    if at is None:
        return 0.0
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max((now - at).total_seconds(), 0.0)


def _bucket_for_depth(depth: int) -> str:
    if depth <= 0:
        return "bucket_0"
    if depth <= 2:
        return "bucket_1_2"
    if depth <= 4:
        return "bucket_3_4"
    return "bucket_gt4"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered), max(1, int(0.95 * len(ordered) + 0.999)))
    return ordered[index - 1]


def _fail_rate(ok: int | float, fail: int | float) -> float:
    total = ok + fail
    return round(fail / total, 4) if total else 0.0


def build_monitor_snapshot(
    session: Session,
    registry: MetricsRegistry,
    settings: Settings,
    *,
    now: datetime,
    audit: AuditService | None = None,
) -> dict[str, Any]:
    tasks = list(session.execute(select(TaskQueue)).scalars())
    users = list(session.execute(select(UserQueue)).scalars())
    metrics = registry.snapshot()

    queue_counts: dict[str, int] = {state: 0 for state in _QUEUE_STATES}
    readies_by_key: dict[str, int] = {}
    for row in tasks:
        state = str(row.queue_state)
        if state in queue_counts:
            queue_counts[state] += 1
        if state == "READY":
            readies_by_key[row.queue_key] = readies_by_key.get(row.queue_key, 0) + 1

    # 等待分桶：每任务等待 = now - updated_at；桶按该 queue_key 的 READY 前序深度划分
    waits: dict[str, dict[str, list[float]]] = {bucket: {} for bucket in _WAIT_BUCKETS}
    for row in tasks:
        state = str(row.queue_state)
        if state not in _ACTIVE_STATES:
            continue
        wait = round(_elapsed(now, row.updated_at), 1)
        bucket = _bucket_for_depth(readies_by_key.get(row.queue_key, 0))
        waits[bucket].setdefault(row.queue_key, []).append(wait)

    fairness: dict[str, dict[str, Any]] = {}
    for bucket, by_key in waits.items():
        per_key_p95 = {key: round(_p95(values), 1) for key, values in by_key.items()}
        queue_keys = sorted(per_key_p95)
        gap = 0.0
        if per_key_p95:
            gap = round(max(per_key_p95.values()) - min(per_key_p95.values()), 1)
        fairness[bucket] = {
            "queue_keys": queue_keys,
            "per_key_p95_seconds": {key: per_key_p95[key] for key in queue_keys},
            "gap_seconds": max(gap, 0.0),
        }

    paused_count = 0
    paused_sustained = 0.0
    for user_row in users:
        if str(user_row.state) == "PAUSED_AUTH":
            paused_count += 1
            paused_sustained = max(paused_sustained, _elapsed(now, user_row.updated_at))
    waiting_timeout_seconds = settings.ai.waiting_timeout_seconds
    waiting_timeouts = sum(
        1
        for row in tasks
        if str(row.queue_state) == "WAITING"
        and _elapsed(now, row.updated_at) > waiting_timeout_seconds
    )
    runtime = {
        "queue_counts": queue_counts,
        "paused_auth": {
            "count": paused_count,
            "sustained_seconds": round(paused_sustained, 1),
        },
        "waiting_timeouts": waiting_timeouts,
    }

    pending_callbacks = [
        row for row in tasks if str(row.task_type) == "callback" and str(row.queue_state) != "DONE"
    ]
    callback_backlog = max(
        (_elapsed(now, row.updated_at) for row in pending_callbacks), default=0.0
    )
    callbacks = {
        "pending": len(pending_callbacks),
        "backlog_seconds": round(callback_backlog, 1),
    }

    width = settings.queue.ai_worker_concurrency
    ai_running = queue_counts["AI_RUNNING"]
    utilization = round(min(ai_running / width, 1.0), 4)
    workers = {"ai_running": ai_running, "width_w": width, "utilization": utilization}

    storage = settings.storage
    fraction = disk_fraction(storage.data_dir, storage.data_dir / "rawlogs", storage.l2_quota_bytes)
    watermark_level = level_for(
        fraction, storage.watermark_warn_fraction, storage.watermark_force_fraction
    )
    watermark = {
        "level": watermark_level.value,
        "fraction": round(fraction, 4),
        "warn_fraction": storage.watermark_warn_fraction,
        "force_fraction": storage.watermark_force_fraction,
    }

    verified = audit.verify_chain() if audit is not None else None

    per_token_depth: dict[str, int] = {}
    for row in tasks:
        if str(row.queue_state) in ("PENDING", "READY"):
            per_token_depth[row.queue_key] = per_token_depth.get(row.queue_key, 0) + 1
    token_ok_300s = int(metrics.get("token_fetch_ok:count_300s", 0))
    token_fail_300s = int(metrics.get("token_fetch_fail:count_300s", 0))
    callback_ok_300s = int(metrics.get("callback_ok:count_300s", 0))
    callback_fail_300s = int(metrics.get("callback_fail:count_300s", 0))
    alerts_eligible = {
        "global_depth": queue_counts["PENDING"] + queue_counts["READY"],
        "per_token_depth": per_token_depth,
        "per_token_depth_max": max(per_token_depth.values(), default=0),
        "token_fetch_ok_300s": token_ok_300s,
        "token_fetch_fail_300s": token_fail_300s,
        "token_fetch_fail_rate": _fail_rate(token_ok_300s, token_fail_300s),
        "callback_ok_300s": callback_ok_300s,
        "callback_fail_300s": callback_fail_300s,
        "callback_fail_rate": _fail_rate(callback_ok_300s, callback_fail_300s),
        "callback_backlog_seconds": callbacks["backlog_seconds"],
        "callback_drop_total": int(metrics.get("callback_drop_total", 0)),
        "memory_materialize_fail_300s": int(metrics.get("memory_materialize_fail:count_300s", 0)),
        "fencing_reject_total": int(metrics.get("fencing_reject_total", 0)),
        "worker_utilization": utilization,
        "waiting_timeouts": waiting_timeouts,
        "watermark_level": watermark["level"],
        "audit_verified": verified,
    }

    return {
        "generated_at": now.isoformat(),
        "runtime": runtime,
        "waits": waits,
        "fairness": fairness,
        "alerts_eligible": alerts_eligible,
        "watermark": watermark,
        "callbacks": callbacks,
        "workers": workers,
        "audit": {"verified": verified},
    }
