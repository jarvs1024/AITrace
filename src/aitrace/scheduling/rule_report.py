# src/aitrace/scheduling/rule_report.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, Stage
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue


def write_rule_report(
    session: Session,
    *,
    task: TaskQueue,
    round_row: Round,
    session_row: SessionModel,
    now: datetime,
    ids: Any,
    category: str,
    detail: str,
    referenced_logs: list[dict[str, Any]],
    gap_note: str | None = None,
) -> None:
    report = Report(
        report_id=ids.new("RP"),
        round_id=round_row.round_id,
        fault_info={
            "product": task.product_id,
            "symptom": detail,
            "env_topology": {"host_ip": (session_row.env_topology or {}).get("host_ip", "-")},
        },
        result={
            "root_cause": {"category": category, "detail": detail, "evidence": []},
            "solution": None,
            "confidence": None,
            "gap_note": gap_note,
        },
        confidence_breakdown=[],
        evidence=[],
        referenced_logs=referenced_logs,
        process_steps=[
            {
                "step": "ROUTING",
                "title": "路由闸规则分类",
                "start": now.isoformat(),
                "end": now.isoformat(),
                "duration_ms": 0,
                "summary": detail,
                "status": "SUCCESS",
            }
        ],
        citation={"citing_status": "NONE"},
        gap_note=gap_note,
        created_at=now,
    )
    session.add(report)
    round_row.report_id = report.report_id
    round_row.stage = Stage.COMPLETED.value
    round_row.round_status = "AWAITING_FEEDBACK"
    round_row.finished_at = now
    task.queue_state = QueueState.DONE.value
    task.dispatch_phase = None
    task.lease_owner = None
    task.lease_until = datetime(1970, 1, 1, tzinfo=now.tzinfo)
    task.updated_at = now
    session.flush()
