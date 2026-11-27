"""任务流水阶段事件：队列状态迁移 → 结构化日志 + 进程内直方图。

SLO 聚合（§7.1）的数据源之一：token 队列等待/清洗耗时/AI 时长的
离线标定从 task_stage 日志聚合，实时监控台从 registry 快照读取。
"""

from __future__ import annotations

from aitrace.observability.logging import log_event
from aitrace.observability.metrics import MetricsRegistry

_STAGE_HISTOGRAM = "task_stage_duration_seconds"


def record_stage_event(
    registry: MetricsRegistry | None,
    *,
    task_type: str,
    queue_key: str,
    from_state: str,
    to_state: str,
    duration_seconds: float,
) -> None:
    if registry is not None:
        registry.histogram(_STAGE_HISTOGRAM).observe(duration_seconds)
    log_event(
        "pipeline",
        "task_stage",
        level="INFO",
        task_type=task_type,
        queue_key=queue_key,
        from_state=from_state,
        to_state=to_state,
        duration_seconds=duration_seconds,
    )
