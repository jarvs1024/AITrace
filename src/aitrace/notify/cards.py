# src/aitrace/notify/cards.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_HIGH_RISK_KEYWORDS = ("FW ASSERT", "AER", "CXL Link Down")
_STATUS_ORDER = ("VALID", "INVALID", "AWAITING_FEEDBACK", "FAILED", "RUNNING")


@dataclass(frozen=True)
class NotifyConfig:
    webhook: str
    at_users: list[str] = field(default_factory=list)
    high_risk_webhook: str | None = None


def detect_high_risk(text: str) -> list[str]:
    """高危关键词判定（大小写不敏感）：命中 FW ASSERT / AER / CXL Link Down 任一。"""
    upper = text.upper()
    return [keyword for keyword in _HIGH_RISK_KEYWORDS if keyword.upper() in upper]


def build_card(
    event: str,
    *,
    batch_id: str,
    product_id: str = "",
    case_id: str = "",
    counts: dict[str, Any] | None = None,
    conclusions: list[dict[str, Any]] | None = None,
    negated: list[str] | None = None,
    detail_url: str = "",
    at_users: list[str] | None = None,
    keywords: list[str] | None = None,
    detail: str = "",
    batch_state: str = "",
) -> dict[str, Any]:
    counts = counts or {}
    conclusions = conclusions or []
    negated = negated or []
    lines: list[str] = []

    def _conclusion_lines() -> None:
        for item in conclusions:
            confidence = item.get("confidence")
            confidence_text = f"{confidence}" if confidence is not None else "—"
            lines.append(
                f"- 案例 {item.get('case_id', '-')}：{item.get('category', 'OTHER')}"
                f"（置信度 {confidence_text}）"
            )

    if event == "batch_card":
        title = f"知因分析 · {product_id} · 批次 {batch_id} 诊断通知"
        lines.append(f"### 知因分析 · {product_id} · 批次 {batch_id}")
        lines.append(f"- 已出结论：{int(counts.get('completed', 0))} 例")
        _conclusion_lines()
        for message in negated:
            lines.append(f"- 否定追加：{message}")
        if detail_url:
            lines.append(f"[详情]({detail_url})")
    elif event == "high_risk":
        title = f"【高危】知因分析 · {product_id} · 批次 {batch_id}"
        lines.append(f"### 高危告警 · 案例 {case_id}")
        lines.append(f"- 命中关键词：{'、'.join(keywords or [])}")
        if detail:
            lines.append(f"- 结论摘要：{detail[:500]}")
        if detail_url:
            lines.append(f"[详情]({detail_url})")
    elif event == "final":
        title = f"知因分析 · {product_id} · 批次 {batch_id} 终态汇总"
        lines.append(f"### 知因分析 · {product_id} · 批次 {batch_id} 终态汇总")
        if batch_state:
            lines.append(f"- 批次状态：{batch_state}")
        for status in _STATUS_ORDER:
            lines.append(f"- 状态 {status}：{int(counts.get(status, 0))} 例")
        _conclusion_lines()
        for message in negated:
            lines.append(f"- 否定追加：{message}")
        if detail_url:
            lines.append(f"[详情]({detail_url})")
    else:
        title = f"知因分析 · {product_id} · 批次 {batch_id} 结论被否定"
        lines.append("### 引用结论被否定")
        lines.extend(f"- {message}" for message in negated)
        if detail_url:
            lines.append(f"[详情]({detail_url})")
    card: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "\n".join(lines)},
    }
    if at_users:
        card["at"] = {"atMobiles": at_users, "isAtAll": False}
    return card
