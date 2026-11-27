import pytest

from aitrace.ai.validate import ConclusionValidationError, extract_json, validate_conclusion

_CATS = ("TEST_ENV_ERROR", "FW_DEFECT", "SCRIPT_ISSUE", "HW_DEFECT", "OTHER")
_HASHES = {"abc123", "def456"}


def _valid() -> dict:
    return {
        "root_cause_category": "FW_DEFECT",
        "root_cause_detail": "固件 gpf phase2 超时",
        "solution": "升级固件并复测",
        "evidence": [
            {
                "source_log": "error_window.txt",
                "slice_path": "evidence/s1/round_1/error_window.txt",
                "slice_hash": "abc123",
                "time_window": "T0-5s..T0+10s",
                "excerpt": "ASSERT gpf phase2 timeout",
            }
        ],
        "gap_note": None,
    }


def test_valid_conclusion_passes() -> None:
    result = validate_conclusion(_valid(), allowed_categories=_CATS, valid_hashes=_HASHES)
    assert result.category == "FW_DEFECT"
    assert result.detail == "固件 gpf phase2 超时"
    assert len(result.evidence) == 1


def test_category_outside_closed_set_rejected() -> None:
    payload = _valid()
    payload["root_cause_category"] = "SOFTWARE_BUG"
    with pytest.raises(ConclusionValidationError) as exc_info:
        validate_conclusion(payload, allowed_categories=_CATS, valid_hashes=_HASHES)
    message = str(exc_info.value)
    assert "root_cause_category" in message
    assert "SOFTWARE_BUG" in message
    assert "TEST_ENV_ERROR" in message


def test_missing_category_rejected() -> None:
    payload = _valid()
    del payload["root_cause_category"]
    with pytest.raises(ConclusionValidationError):
        validate_conclusion(payload, allowed_categories=_CATS, valid_hashes=_HASHES)


def test_evidence_hash_must_exist_in_l1() -> None:
    payload = _valid()
    payload["evidence"][0]["slice_hash"] = "not-in-l1"
    with pytest.raises(ConclusionValidationError) as exc_info:
        validate_conclusion(payload, allowed_categories=_CATS, valid_hashes=_HASHES)
    message = str(exc_info.value)
    assert "slice_hash" in message
    assert "not-in-l1" in message


def test_empty_detail_rejected() -> None:
    payload = _valid()
    payload["root_cause_detail"] = ""
    with pytest.raises(ConclusionValidationError):
        validate_conclusion(payload, allowed_categories=_CATS, valid_hashes=_HASHES)


def test_extract_json_strips_code_fences_and_prose() -> None:
    text = '好的，结论如下：\n```json\n{"root_cause_category": "OTHER"}\n```\n以上。'
    assert extract_json(text) == {"root_cause_category": "OTHER"}


def test_extract_json_invalid_raises_chinese_error() -> None:
    with pytest.raises(ConclusionValidationError, match="JSON"):
        extract_json("没有 JSON 输出")
