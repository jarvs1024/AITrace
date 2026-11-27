from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aitrace.core.enums import QueueState, Stage, WaitingReason

_MESSAGES = {
    Stage.QUEUED: "排队等待分析中...",
    Stage.CLEANING: "正在进行多源日志脱水、首错定位、三段切片与时间对齐...",
    Stage.AI_CLASSIFYING: "Qoder 主引擎正在全局归类并定界...",
    Stage.DEEP_PROBING: "正在检索源码并实查 SSD/CXL 状态...",
    Stage.COMPLETED: "知因分析完成（点击查看根因报告）",
    Stage.FAILED: "分析异常（支持一键重新触发）",
}


@dataclass(frozen=True)
class StageView:
    stage: str
    progress: int | None
    waiting_reason: str | None
    message: str


def build_stage_view(task: Any, queue: Any, now: datetime) -> StageView:
    state = QueueState(task.queue_state)
    if state is QueueState.FAILED:
        return StageView(Stage.FAILED, None, None, _MESSAGES[Stage.FAILED])
    if state is QueueState.DONE:
        return StageView(Stage.COMPLETED, 100, None, _MESSAGES[Stage.COMPLETED])
    if state is QueueState.CLEANING:
        return StageView(Stage.CLEANING, 30, None, _MESSAGES[Stage.CLEANING])
    if state is QueueState.AI_RUNNING and task.dispatch_phase == "AI_EXEC":
        return StageView(Stage.AI_CLASSIFYING, 50, None, _MESSAGES[Stage.AI_CLASSIFYING])

    reason = _waiting_reason(task, queue, now)
    return StageView(Stage.QUEUED, 10, reason, _MESSAGES[Stage.QUEUED])


def _waiting_reason(task: Any, queue: Any, now: datetime) -> str | None:
    if queue.state == "PAUSED_AUTH":
        return WaitingReason.PAUSED_AUTH
    if task.next_retry_at is not None and task.next_retry_at > now:
        return WaitingReason.RETRY_BACKOFF
    state = QueueState(task.queue_state)
    if state is QueueState.WAITING:
        if task.not_before is not None and task.not_before > now:
            return WaitingReason.NOT_BEFORE
        return WaitingReason.CITATION_PENDING
    if state is QueueState.AI_RUNNING:
        return WaitingReason.TOKEN_BUSY
    if state is QueueState.READY:
        return WaitingReason.TOKEN_BUSY
    return None
