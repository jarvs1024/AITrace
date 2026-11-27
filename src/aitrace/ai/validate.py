from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_CODE_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ConclusionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedConclusion:
    category: str
    detail: str
    solution: str | None
    evidence: tuple[dict[str, Any], ...]
    gap_note: str | None


def extract_json(text: str) -> dict[str, Any]:
    match = _CODE_FENCE.search(text)
    candidate = match.group(1) if match else text
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        # 真实 SDK 输出常为「散文 + 纯 JSON」：回退取首个 { 到末个 } 的子串解析。
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ConclusionValidationError(
                "AI 输出解析失败：输出中未找到 JSON 对象，"
                f"当前输出前 200 字：{text[:200]}；要求：AI 必须输出契约约定的 JSON 对象"
            ) from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ConclusionValidationError(
                f"AI 输出解析失败：输出不是合法 JSON（{exc.msg}），"
                "要求：AI 必须只输出契约约定的 JSON 对象"
            ) from exc
    if not isinstance(data, dict):
        raise ConclusionValidationError(
            "AI 输出解析失败：JSON 顶层必须是对象；当前值类型："
            f"{type(data).__name__}；要求：输出契约约定的 JSON 对象"
        )
    return data


def validate_conclusion(
    payload: dict[str, Any],
    *,
    allowed_categories: Sequence[str],
    valid_hashes: set[str],
) -> ValidatedConclusion:
    category = payload.get("root_cause_category")
    if not isinstance(category, str) or not category:
        raise ConclusionValidationError(
            f"AI 结论校验失败：root_cause_category（根因归类）当前值：{category!r}；"
            f"要求：必须且只能取闭集枚举之一：{'、'.join(allowed_categories)}"
        )
    if category not in allowed_categories:
        raise ConclusionValidationError(
            f"AI 结论校验失败：root_cause_category（根因归类）当前值：{category!r}；"
            f"要求：必须且只能取闭集枚举之一：{'、'.join(allowed_categories)}"
        )
    detail = payload.get("root_cause_detail")
    if not isinstance(detail, str) or not detail.strip():
        raise ConclusionValidationError(
            f"AI 结论校验失败：root_cause_detail（定界结论说明）当前值：{detail!r}；"
            "要求：非空中文说明"
        )
    evidence_raw = payload.get("evidence")
    if evidence_raw is None:
        evidence_raw = []
    if not isinstance(evidence_raw, list):
        raise ConclusionValidationError(
            f"AI 结论校验失败：evidence（证据数组）当前值类型：{type(evidence_raw).__name__}；"
            "要求：数组（可为空）"
        )
    evidence: list[dict[str, Any]] = []
    for index, item in enumerate(evidence_raw):
        if not isinstance(item, dict):
            raise ConclusionValidationError(
                f"AI 结论校验失败：evidence[{index}]（证据条目）当前值类型："
                f"{type(item).__name__}；要求：对象"
            )
        evidence_hash = item.get("slice_hash")
        if not isinstance(evidence_hash, str) or evidence_hash not in valid_hashes:
            raise ConclusionValidationError(
                f"AI 结论校验失败：evidence[{index}].slice_hash（切片哈希）当前值："
                f"{evidence_hash!r}；要求：必须引用真实存在的 L1 切片 SHA-256"
            )
        evidence.append(item)
    solution = payload.get("solution")
    gap_note = payload.get("gap_note")
    return ValidatedConclusion(
        category=category,
        detail=detail,
        solution=solution if isinstance(solution, str) else None,
        evidence=tuple(evidence),
        gap_note=gap_note if isinstance(gap_note, str) else None,
    )
