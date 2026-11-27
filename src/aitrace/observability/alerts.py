# src/aitrace/observability/alerts.py
"""告警判定纯函数与运维告警群投递。

evaluate_alerts 只读监控快照与配置，除 fencing「持续」判定需要跨巡检周期的
prev_state（由 next_alert_state 推进）外，同一输入产出同一告警列表。
投递文案：【AITrace告警】{severity} {name}：{message}（阈值 {threshold}，当前 {current}）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from aitrace.core.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    name: str
    severity: str
    threshold: str
    current: str
    message: str


def _shot_ok_fail(alerts_eligible: dict[str, Any], prefix: str) -> tuple[int, int]:
    return (
        int(alerts_eligible.get(f"{prefix}_ok_300s", 0)),
        int(alerts_eligible.get(f"{prefix}_fail_300s", 0)),
    )


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fencing_view(
    snapshot: dict[str, Any], prev_state: dict[str, Any] | None, now: datetime
) -> tuple[bool, float | None]:
    """返回 (本周期仍在增长, 增长起始 epoch 秒)；无增长时起点为 None。"""
    cur = int(snapshot["alerts_eligible"].get("fencing_reject_total", 0))
    prev = prev_state or {}
    prev_count = int(prev.get("fencing_count", 0))
    if cur <= prev_count:
        return False, None
    started = prev.get("fencing_started_at")
    return True, float(started) if started is not None else now.timestamp()


def evaluate_alerts(
    snapshot: dict[str, Any],
    settings: Settings,
    *,
    now: datetime,
    prev_state: dict[str, Any] | None = None,
) -> list[Alert]:
    alerts: list[Alert] = []

    eligible: dict[str, Any] = snapshot.get("alerts_eligible", {})
    paused: dict[str, Any] = snapshot.get("runtime", {}).get("paused_auth", {})
    queue_counts: dict[str, Any] = snapshot.get("runtime", {}).get("queue_counts", {})
    monitor = settings.monitor

    # 队列深度（全局/单令牌）
    global_depth = int(eligible.get("global_depth", 0))
    global_warn = int(settings.queue.global_depth * monitor.global_depth_warn_fraction)
    if global_depth >= global_warn:
        alerts.append(
            Alert(
                name="global_depth_warn",
                severity="WARN",
                threshold=f"{global_warn} 条",
                current=f"{global_depth} 条",
                message="全局待处理任务深度达到告警水位，请关注消费速率",
            )
        )
    per_token_depth: dict[str, Any] = eligible.get("per_token_depth", {})
    for queue_key, depth in per_token_depth.items():
        if int(depth) > monitor.per_token_depth_warn:
            alerts.append(
                Alert(
                    name="per_token_depth_warn",
                    severity="WARN",
                    threshold=f"{monitor.per_token_depth_warn} 条",
                    current=f"{int(depth)} 条",
                    message=f"单令牌队列深度超阈值（queue_key={queue_key}）",
                )
            )

    # 授权暂停持续
    paused_sustained = float(paused.get("sustained_seconds", 0.0))
    if paused_sustained > monitor.paused_auth_warn_seconds:
        alerts.append(
            Alert(
                name="paused_auth_sustained",
                severity="WARN",
                threshold=f"{monitor.paused_auth_warn_seconds} 秒",
                current=f"{round(paused_sustained, 1)} 秒",
                message=(
                    f"用户队列授权暂停持续超阈值（{int(paused.get('count', 0))} 个队列），"
                    "请确认令牌授权状态"
                ),
            )
        )

    # 令牌拉取失败率（窗口内）
    token_ok, token_fail = _shot_ok_fail(eligible, "token_fetch")
    token_rate = float(eligible.get("token_fetch_fail_rate", 0.0) or 0.0)
    if (token_ok + token_fail) > 0 and token_rate > monitor.token_fetch_fail_rate:
        alerts.append(
            Alert(
                name="token_fetch_fail_rate_warn",
                severity="WARN",
                threshold=_percent(monitor.token_fetch_fail_rate),
                current=_percent(token_rate),
                message="令牌拉取失败率超阈值，请检查 AI 网关连通性",
            )
        )

    # 回调失败率 / 积压
    callback_ok, callback_fail = _shot_ok_fail(eligible, "callback")
    callback_rate = float(eligible.get("callback_fail_rate", 0.0) or 0.0)
    if (callback_ok + callback_fail) > 0 and callback_rate > monitor.callback_fail_rate:
        alerts.append(
            Alert(
                name="callback_fail_rate_warn",
                severity="WARN",
                threshold=_percent(monitor.callback_fail_rate),
                current=_percent(callback_rate),
                message="结果回调失败率超阈值，请检查 aitest 服务可用性",
            )
        )
    backlog = float(eligible.get("callback_backlog_seconds", 0.0))
    if backlog > monitor.callback_backlog_seconds:
        alerts.append(
            Alert(
                name="callback_backlog_warn",
                severity="WARN",
                threshold=f"{monitor.callback_backlog_seconds} 秒",
                current=f"{round(backlog, 1)} 秒",
                message="回调积压时长超阈值，请检查回调投递链路",
            )
        )

    # 热记忆物化失败（窗口内）
    materialize_fail = int(eligible.get("memory_materialize_fail_300s", 0))
    if materialize_fail > 0:
        alerts.append(
            Alert(
                name="memory_materialize_fail",
                severity="WARN",
                threshold="0 次",
                current=f"{materialize_fail} 次",
                message=f"近 300 秒热记忆物化失败 {materialize_fail} 次",
            )
        )

    # WAITING 超时转换数
    waiting_timeouts = int(eligible.get("waiting_timeouts", 0))
    if waiting_timeouts > 0:
        alerts.append(
            Alert(
                name="waiting_timeouts",
                severity="WARN",
                threshold="0 个",
                current=f"{waiting_timeouts} 个",
                message=f"有 {waiting_timeouts} 个任务等待远超超时上限，请检查调度",
            )
        )

    # Worker 利用率（低仅在有待处理工作时告警，避免空闲时段误报）
    utilization = float(eligible.get("worker_utilization", 0.0) or 0.0)
    pending_work = int(queue_counts.get("PENDING", 0)) + int(queue_counts.get("READY", 0))
    if utilization < monitor.worker_util_low and pending_work > 0:
        alerts.append(
            Alert(
                name="worker_util_low",
                severity="WARN",
                threshold=f"{monitor.worker_util_low * 100:.0f}%",
                current=f"{utilization * 100:.0f}%",
                message="AI Worker 利用率低于下限且队列有待处理任务",
            )
        )
    if utilization >= monitor.worker_util_high:
        alerts.append(
            Alert(
                name="worker_util_high",
                severity="WARN",
                threshold=f"{monitor.worker_util_high * 100:.0f}%",
                current=f"{utilization * 100:.0f}%",
                message="AI Worker 利用率达上限，处理能力接近饱和",
            )
        )

    # 磁盘水位（比例在快照 watermark 段，阈值在 storage 配置）
    level = str(eligible.get("watermark_level", "NORMAL"))
    fraction = float(snapshot.get("watermark", {}).get("fraction", 0.0) or 0.0)
    if level == "FORCE":
        alerts.append(
            Alert(
                name="watermark_force",
                severity="CRITICAL",
                threshold=_percent(settings.storage.watermark_force_fraction),
                current=_percent(fraction),
                message="磁盘水位进入强制清理档，存储即将耗尽",
            )
        )
    elif level == "WARN":
        alerts.append(
            Alert(
                name="watermark_warn",
                severity="WARN",
                threshold=_percent(settings.storage.watermark_warn_fraction),
                current=_percent(fraction),
                message="磁盘水位进入告警档，请关注容量",
            )
        )

    # 审计哈希链
    if snapshot.get("audit", {}).get("verified") is False:
        alerts.append(
            Alert(
                name="audit_chain_broken",
                severity="CRITICAL",
                threshold="哈希链完整",
                current="校验失败",
                message="审计哈希链校验失败，审计写入已熔断，请立即排查",
            )
        )

    # fencing 持续拒绝（跨巡检周期状态，见 next_alert_state）
    growing, started = _fencing_view(snapshot, prev_state, now)
    if growing and started is not None:
        sustained = now.timestamp() - started
        if sustained >= monitor.fencing_reject_warn_seconds:
            alerts.append(
                Alert(
                    name="fencing_reject_sustained",
                    severity="WARN",
                    threshold=f"{monitor.fencing_reject_warn_seconds} 秒",
                    current=f"{sustained:.0f} 秒",
                    message="租约 fencing 拒绝持续发生，疑似多写者竞争或任务超时",
                )
            )

    return alerts


def next_alert_state(
    snapshot: dict[str, Any], prev_state: dict[str, Any] | None, now: datetime
) -> dict[str, Any]:
    """推进跨巡检周期的告警状态（当前仅 fencing 持续判定）。"""
    cur = int(snapshot["alerts_eligible"].get("fencing_reject_total", 0))
    prev = prev_state or {}
    prev_count = int(prev.get("fencing_count", 0))
    if cur > prev_count:
        started = prev.get("fencing_started_at")
        return {
            "fencing_count": cur,
            "fencing_started_at": float(started) if started is not None else now.timestamp(),
        }
    return {"fencing_count": cur, "fencing_started_at": None}


def send_alerts(
    alerts: list[Alert],
    *,
    ops_webhook: str | None,
    poster: Callable[[str, str], None],
) -> None:
    if not alerts or not ops_webhook:
        return
    for alert in alerts:
        text = (
            f"【AITrace告警】{alert.severity} {alert.name}：{alert.message}"
            f"（阈值 {alert.threshold}，当前 {alert.current}）"
        )
        try:
            poster(ops_webhook, text)
        except Exception:
            logger.warning("告警投递失败 name=%s severity=%s", alert.name, alert.severity)


def dingtalk_text_poster(webhook: str, text: str) -> None:
    """钉钉自定义机器人文本消息投递；失败抛异常由 send_alerts 降级为 WARN 日志。"""
    response = httpx.post(
        webhook,
        json={"msgtype": "text", "text": {"content": text}},
        timeout=5.0,
    )
    response.raise_for_status()
