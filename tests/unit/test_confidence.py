from aitrace.ai.confidence import (
    AiStats,
    ConfidenceDims,
    ReferenceHit,
    compute_dims,
    score_confidence,
)
from aitrace.profiles.models import ConfidenceWeights


def _metrics(**over) -> dict:
    base = {
        "alignment_rate": 1.0,
        "per_source": {
            "test": {"original_bytes": 10, "slice_bytes": 10, "events_parsed": 5},
            "uart": {"original_bytes": 10, "slice_bytes": 10, "events_parsed": 5},
        },
    }
    base.update(over)
    return base


def _summary(**over) -> dict:
    base = {
        "status": "ready",
        "source_availability": {"test": True, "uart": True},
        "first_error": {
            "exc_type": "AssertionError",
            "err_code": "0x10B",
            "excerpt": "reg0=0xDEAD",
        },
        "anchor_utc": "2026-11-20T10:53:40.000Z",
        "anchor_source": "uart",
    }
    base.update(over)
    return base


def test_full_marks_dims_and_score() -> None:
    dims = compute_dims(
        metrics=_metrics(),
        summary=_summary(),
        ai_stats=AiStats(source_review="DONE", probe_success=True, probe_age_minutes=5),
        reference=ReferenceHit(kind="lesson"),
        probing_done=True,
        consistent=True,
    )
    assert dims.log_completeness == 1.0
    assert dims.consistency_check == 1.0
    result = score_confidence(dims, ConfidenceWeights(), hard_caps={})
    assert result.score == 1.0
    assert len(result.breakdown) == 7
    assert result.gap_note is None


def test_missing_sources_and_alignment_scale_down() -> None:
    dims = compute_dims(
        metrics=_metrics(alignment_rate=0.5),
        summary=_summary(source_availability={"test": True, "uart": False}),
        ai_stats=AiStats(),
        reference=ReferenceHit(),
        probing_done=False,
        consistent=None,
    )
    assert dims.log_completeness == 0.5
    assert dims.time_alignment == 0.5  # 0.5 对齐率 + 无加分
    assert dims.consistency_check == 0.5  # 未探查


def test_process_error_rate_penalty() -> None:
    dims = compute_dims(
        metrics=_metrics(),
        summary=_summary(),
        ai_stats=AiStats(tool_calls=10, tool_errors=3),
        reference=ReferenceHit(),
        probing_done=False,
        consistent=None,
    )
    assert dims.process_error_rate == 0.0  # 30% > 20% 记 0
    dims2 = compute_dims(
        metrics=_metrics(),
        summary=_summary(),
        ai_stats=AiStats(tool_calls=10, tool_errors=1),
        reference=ReferenceHit(),
        probing_done=False,
        consistent=None,
    )
    assert dims2.process_error_rate == 0.5  # 10% 线性：1 - 0.1/0.2


def test_reference_hit_mapping_and_no_control() -> None:
    dims = compute_dims(
        metrics=_metrics(),
        summary=_summary(),
        ai_stats=AiStats(),
        reference=ReferenceHit(kind="valid"),
        probing_done=False,
        consistent=None,
    )
    assert dims.reference_hit == 0.7
    dims2 = compute_dims(
        metrics=_metrics(),
        summary=_summary(),
        ai_stats=AiStats(),
        reference=ReferenceHit(kind="none", no_control=True),
        probing_done=False,
        consistent=None,
    )
    assert dims2.reference_hit == 0.3


def test_hard_caps_apply() -> None:
    dims = ConfidenceDims(1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    result = score_confidence(
        dims,
        ConfidenceWeights(),
        hard_caps={"no_anchor": True},
    )
    assert result.score == 0.3
    assert result.gap_note is not None
    assert "锚点" in result.gap_note


def test_other_category_cap_and_gap_note() -> None:
    dims = ConfidenceDims(0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9)
    result = score_confidence(dims, ConfidenceWeights(), hard_caps={"other_category": True})
    assert result.score == 0.5
    assert result.gap_note is not None


def test_low_score_forces_gap_note() -> None:
    dims = ConfidenceDims(0.0, 0.0, 0.0, 0.0, 0.0, 0.3, 0.2)
    result = score_confidence(dims, ConfidenceWeights(), hard_caps={})
    assert result.score < 0.5
    assert result.gap_note is not None
    assert "为什么低" in result.gap_note or "缺" in result.gap_note


def test_score_rounds_two_decimals() -> None:
    dims = ConfidenceDims(0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7)
    result = score_confidence(dims, ConfidenceWeights(), hard_caps={})
    assert result.score == round(result.score, 2)
