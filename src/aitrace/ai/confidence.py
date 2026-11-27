# src/aitrace/ai/confidence.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aitrace.profiles.models import ConfidenceWeights


@dataclass(frozen=True)
class AiStats:
    tool_calls: int = 0
    tool_errors: int = 0
    source_review: str = "SKIPPED"  # DONE / SKIPPED / FAILED
    probe_success: bool | None = None
    probe_age_minutes: float | None = None


@dataclass(frozen=True)
class ReferenceHit:
    kind: str = "none"  # none / valid / lesson / conflict
    no_control: bool = False


@dataclass(frozen=True)
class ConfidenceDims:
    log_completeness: float
    time_alignment: float
    error_info: float
    process_error_rate: float
    evidence_chain: float
    reference_hit: float
    consistency_check: float


@dataclass(frozen=True)
class ConfidenceResult:
    score: float
    breakdown: tuple[dict[str, Any], ...]
    gap_note: str | None


_DIM_LABELS = (
    ("log_completeness", "日志完备度"),
    ("time_alignment", "时间对齐度"),
    ("error_info", "错误信息完备度"),
    ("process_error_rate", "分析过程错误率"),
    ("evidence_chain", "证据链完备度"),
    ("reference_hit", "参考信息命中"),
    ("consistency_check", "一致性核验"),
)


def compute_dims(
    *,
    metrics: dict[str, Any],
    summary: dict[str, Any],
    ai_stats: AiStats,
    reference: ReferenceHit,
    probing_done: bool,
    consistent: bool | None,
) -> ConfidenceDims:
    availability = summary.get("source_availability") or {}
    enabled = [k for k, v in availability.items()]
    completeness = sum(1 for v in availability.values() if v) / len(enabled) if enabled else 0.0

    alignment_rate = float(metrics.get("alignment_rate") or 0.0)
    time_alignment = min(alignment_rate, 1.0)
    if summary.get("anchor_source") == "uart" and availability.get("uart"):
        time_alignment = min(time_alignment + 0.2, 1.0)
    first = summary.get("first_error") or {}
    anchor_utc = summary.get("anchor_utc")
    first_ts = first.get("ts_utc") if isinstance(first, dict) else None
    anchor_dt = _parse(anchor_utc) if isinstance(anchor_utc, str) else None
    first_dt = _parse(first_ts) if isinstance(first_ts, str) else None
    if anchor_dt is not None and first_dt is not None and anchor_dt < first_dt:
        time_alignment = min(time_alignment + 0.2, 1.0)

    error_info = 0.0
    if isinstance(first, dict):
        if first.get("exc_type"):
            error_info += 1 / 3
        if first.get("err_code"):
            error_info += 1 / 3
        excerpt = first.get("excerpt") or ""
        if "reg" in excerpt or "exit" in excerpt.lower():
            error_info += 1 / 3
    shrinks = metrics.get("shrinks") or 0
    exemptions = metrics.get("exemptions") or 0
    if exemptions == 0 and shrinks:
        error_info = max(error_info - min(0.1 * shrinks, 0.3), 0.0)

    if ai_stats.tool_calls <= 0:
        process_error = 1.0
    else:
        ratio = ai_stats.tool_errors / ai_stats.tool_calls
        process_error = max(0.0, 1 - ratio / 0.2) if ratio <= 0.2 else 0.0

    source_score = {"DONE": 1.0, "SKIPPED": 0.4, "FAILED": 0.0}.get(ai_stats.source_review, 0.4)
    if probing_done and ai_stats.probe_success is not None:
        fresh = ai_stats.probe_age_minutes is not None and ai_stats.probe_age_minutes <= 30
        probe_score = 1.0 if ai_stats.probe_success and fresh else 0.4
    else:
        probe_score = 0.5
    evidence_chain = 0.5 * source_score + 0.5 * probe_score

    if reference.kind == "lesson":
        reference_hit = 1.0
    elif reference.kind == "valid":
        reference_hit = 0.7
    elif reference.kind == "conflict":
        reference_hit = 0.3
    else:
        reference_hit = 0.5
    if reference.no_control:
        reference_hit = 0.3

    consistency = 0.5 if not probing_done or consistent is None else (1.0 if consistent else 0.2)

    return ConfidenceDims(
        log_completeness=round(completeness, 4),
        time_alignment=round(time_alignment, 4),
        error_info=round(error_info, 4),
        process_error_rate=round(process_error, 4),
        evidence_chain=round(evidence_chain, 4),
        reference_hit=reference_hit,
        consistency_check=consistency,
    )


def score_confidence(
    dims: ConfidenceDims,
    weights: ConfidenceWeights,
    *,
    hard_caps: dict[str, bool],
    low_threshold: float = 0.5,
) -> ConfidenceResult:
    dim_values = {
        "log_completeness": dims.log_completeness,
        "time_alignment": dims.time_alignment,
        "error_info": dims.error_info,
        "process_error_rate": dims.process_error_rate,
        "evidence_chain": dims.evidence_chain,
        "reference_hit": dims.reference_hit,
        "consistency_check": dims.consistency_check,
    }
    weight_values = {
        "log_completeness": weights.log_completeness,
        "time_alignment": weights.time_alignment,
        "error_info": weights.error_info_completeness,
        "process_error_rate": weights.process_error_rate,
        "evidence_chain": weights.evidence_chain,
        "reference_hit": weights.reference_hit,
        "consistency_check": weights.consistency_check,
    }
    score = sum(dim_values[key] * weight_values[key] for key in dim_values)
    score = round(score, 2)
    cap_note = None
    if hard_caps.get("no_anchor"):
        score = min(score, 0.3)
        cap_note = "四路日志全缺或锚点无法确定"
    if hard_caps.get("probe_conflict"):
        score = min(score, 0.4)
        cap_note = "探针实查值与 aitest 权威信息不一致且无法解释"
    if hard_caps.get("other_category"):
        score = min(score, 0.5)
        cap_note = "结论为 OTHER（判据不足）"
    breakdown = tuple(
        {
            "dimension": key,
            "label": label,
            "score": dim_values[key],
            "weight": weight_values[key],
        }
        for key, label in _DIM_LABELS
    )
    gap_note = None
    if score < low_threshold:
        gap_note = (
            f"全链路置信度 {score} 低于阈值 {low_threshold}："
            + (f"{cap_note}；" if cap_note else "")
            + "缺少的关键证据："
            + _missing_dims(dim_values)
        )
    elif cap_note:
        gap_note = f"置信度受硬约束封顶（{cap_note}），结论仅供参考。"
    return ConfidenceResult(score=score, breakdown=breakdown, gap_note=gap_note)


def _missing_dims(dim_values: dict[str, float]) -> str:
    weak = [label for key, label in _DIM_LABELS if dim_values[key] < 0.5]
    return "、".join(weak) if weak else "无（请人工复核）"


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
