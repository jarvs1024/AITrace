import json
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parents[2]


def test_result_callback_matches_schema() -> None:
    schema = json.loads(
        (REPO / "contracts/schemas/status-callback.schema.json").read_text(encoding="utf-8")
    )
    payload = {
        "event": "RESULT",
        "session_id": "S1",
        "batch_id": "B1",
        "case_id": "C1",
        "round_no": 1,
        "seq": 1,
        "batch_seq": 1,
        "stage": "COMPLETED",
        "progress": 100,
        "waiting_reason": None,
        "message": "知因分析完成（点击查看根因报告）",
        "report": {
            "root_cause": {"category": "FW_DEFECT", "detail": "d", "evidence": []},
            "solution": None,
            "confidence": 0.81,
            "gap_note": None,
        },
        "citation": {"citing_status": "NONE"},
        "queue_event": None,
        "sessions": None,
    }
    jsonschema.validate(payload, schema)


def test_progress_callback_matches_schema() -> None:
    schema = json.loads(
        (REPO / "contracts/schemas/status-callback.schema.json").read_text(encoding="utf-8")
    )
    payload = {
        "event": "PROGRESS",
        "session_id": "S1",
        "batch_id": "B1",
        "case_id": "C1",
        "round_no": 1,
        "seq": 2,
        "batch_seq": 2,
        "stage": "AI_CLASSIFYING",
        "progress": 15,
        "waiting_reason": None,
        "message": "Qoder 主引擎正在全局归类并定界（故障模式识别）",
        "report": None,
        "citation": None,
        "queue_event": None,
        "sessions": None,
    }
    jsonschema.validate(payload, schema)


def test_queue_state_callback_matches_schema() -> None:
    schema = json.loads(
        (REPO / "contracts/schemas/status-callback.schema.json").read_text(encoding="utf-8")
    )
    payload = {
        "event": "QUEUE_STATE",
        "session_id": None,
        "batch_id": "B1",
        "case_id": "",
        "round_no": 0,
        "seq": 3,
        "batch_seq": 3,
        "stage": None,
        "progress": None,
        "waiting_reason": None,
        "message": "Qoder 令牌已过期，队列暂停",
        "report": None,
        "citation": None,
        "queue_event": {
            "type": "PAUSED_AUTH",
            "reason": "TOKEN_EXPIRED",
            "affected_tasks": [],
            "count": 0,
            "paused_until": "2026-11-26T08:00:01+00:00",
        },
        "sessions": None,
    }
    jsonschema.validate(payload, schema)
