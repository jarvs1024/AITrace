# tests/integration/test_dashboard_api.py
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from aitrace.persistence.models import (
    CaseLibrary,
    Report,
    Round,
    SessionModel,
    TaskQueue,
    UserQueue,
)
from tests.integration.test_delivery_api import HEADERS

_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)


def _seed_session(
    db: Session,
    *,
    session_id: str,
    product_id: str,
    case_id: str,
    latest_round_no: int,
    rounds: list[dict[str, object]],
) -> None:
    db.add(
        SessionModel(
            session_id=session_id,
            product_id=product_id,
            batch_id=f"BATCH-{case_id}",
            case_id=case_id,
            domain_id="enterprise",
            profile_version="v1-20261120",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            stable_fingerprint=f"fp-{case_id}",
            env_topology={},
            git_context={},
            latest_round_no=latest_round_no,
            status="RUNNING",
            created_at=_NOW,
        )
    )
    db.flush()
    for spec in rounds:
        db.add(
            Round(
                round_id=str(spec["round_id"]),
                session_id=session_id,
                round_no=int(spec["round_no"]),
                trigger_user_id="zhangsan",
                queue_key="zhangsan",
                stage=str(spec["stage"]),
                round_status=str(spec["round_status"]),
                report_id=spec.get("report_id"),
                confidence=spec.get("confidence"),
                started_at=_NOW,
                finished_at=spec.get("finished_at"),
            )
        )
    db.flush()


def _seed_report(db: Session, *, report_id: str, round_id: str, category: str) -> None:
    db.add(
        Report(
            report_id=report_id,
            round_id=round_id,
            fault_info={},
            result={"root_cause": {"category": category, "detail": "根因细节"}},
            confidence_breakdown=[],
            evidence=[],
            referenced_logs=[],
            process_steps=[],
            citation=None,
            gap_note=None,
            created_at=_NOW,
        )
    )
    db.flush()


def _seed_dashboard_world(db: Session) -> None:
    # 产品 ssd_9560：S1 单轮 AWAITING_FEEDBACK（FW_DEFECT，置信 0.8）
    _seed_session(
        db,
        session_id="S-DASH-1",
        product_id="ssd_9560",
        case_id="CASE-D1",
        latest_round_no=1,
        rounds=[
            {
                "round_id": "R-DASH-1",
                "round_no": 1,
                "stage": "COMPLETED",
                "round_status": "AWAITING_FEEDBACK",
                "report_id": "RPT-DASH-1",
                "confidence": 0.8,
                "finished_at": _NOW,
            }
        ],
    )
    _seed_report(db, report_id="RPT-DASH-1", round_id="R-DASH-1", category="FW_DEFECT")
    # S2 两轮：r1 FAILED（置信 0.4，已结束）；r2 VALID（TEST_ENV_ERROR，置信 0.95）
    _seed_session(
        db,
        session_id="S-DASH-2",
        product_id="ssd_9560",
        case_id="CASE-D2",
        latest_round_no=2,
        rounds=[
            {
                "round_id": "R-DASH-2A",
                "round_no": 1,
                "stage": "FAILED",
                "round_status": "FAILED",
                "confidence": 0.4,
                "finished_at": _NOW,
            },
            {
                "round_id": "R-DASH-2B",
                "round_no": 2,
                "stage": "COMPLETED",
                "round_status": "VALID",
                "report_id": "RPT-DASH-2",
                "confidence": 0.95,
                "finished_at": _NOW,
            },
        ],
    )
    _seed_report(db, report_id="RPT-DASH-2", round_id="R-DASH-2B", category="TEST_ENV_ERROR")
    # 产品 cxl_mem：S3 单轮 FAILED（OTHER，置信 0.2）
    _seed_session(
        db,
        session_id="S-DASH-3",
        product_id="cxl_mem",
        case_id="CASE-D3",
        latest_round_no=1,
        rounds=[
            {
                "round_id": "R-DASH-3",
                "round_no": 1,
                "stage": "FAILED",
                "round_status": "FAILED",
                "report_id": "RPT-DASH-3",
                "confidence": 0.2,
                "finished_at": _NOW,
            }
        ],
    )
    _seed_report(db, report_id="RPT-DASH-3", round_id="R-DASH-3", category="OTHER")
    # 案例库两条：use_hits 合计 3
    db.add(
        CaseLibrary(
            domain_id="enterprise",
            stable_fingerprint="fp-LIB-A",
            category="FW_DEFECT",
            ai_conclusion="结论 A",
            evidence_summary="",
            exact_fingerprints=[],
            confirm_count=0,
            use_hits=1,
            observation_hits=0,
            entry_status="valid",
            version=1,
            updated_at=_NOW,
        )
    )
    db.add(
        CaseLibrary(
            domain_id="enterprise",
            stable_fingerprint="fp-LIB-B",
            category="TEST_ENV_ERROR",
            ai_conclusion="结论 B",
            evidence_summary="",
            exact_fingerprints=[],
            confirm_count=0,
            use_hits=2,
            observation_hits=0,
            entry_status="lesson",
            version=1,
            updated_at=_NOW,
        )
    )
    # 五态队列占位行（batch_id 取 task_id 保证 uq_task_batch 不撞）
    for state in ("PENDING", "CLEANING", "READY", "AI_RUNNING", "WAITING"):
        for i in range(2 if state in ("PENDING", "WAITING") else 1):
            task_id = f"T-DASH-{state}-{i}"
            db.add(
                TaskQueue(
                    task_id=task_id,
                    batch_id=task_id,
                    session_id=None,
                    round_id=f"R-{task_id}",
                    round_no=0,
                    product_id="ssd_9560",
                    case_id="",
                    task_type="case_clean",
                    queue_key="zhangsan",
                    queue_state=state,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
    db.add(UserQueue(queue_key="u-active", state="ACTIVE", updated_at=_NOW))
    db.add(UserQueue(queue_key="u-paused", state="PAUSED_AUTH", updated_at=_NOW))
    db.commit()


def test_dashboard_summary_all_products(test_settings: Settings, db_session: Session) -> None:
    _seed_dashboard_world(db_session)
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/dashboard/summary", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] is None
    assert body["generated_at"]
    overview = body["overview"]
    assert overview["session_count"] == 3
    assert overview["round_count"] == 4
    assert overview["valid_rate"] == pytest.approx(1 / 3)
    assert overview["round_distribution"] == {"1": 2, "2": 1, "3+": 0}
    assert overview["feedback_distribution"] == {
        "VALID": 1,
        "INVALID": 0,
        "AWAITING_FEEDBACK": 1,
        "FAILED": 1,
        "RUNNING": 0,
    }
    by_product = {row["product_id"]: row for row in body["by_product"]}
    assert list(by_product) == ["cxl_mem", "ssd_9560"]
    cxl = by_product["cxl_mem"]
    assert cxl["session_count"] == 1
    assert cxl["round_count"] == 1
    assert (cxl["valid"], cxl["invalid"], cxl["awaiting"], cxl["failed"], cxl["running"]) == (
        0,
        0,
        0,
        1,
        0,
    )
    assert cxl["valid_rate"] == 0.0
    ssd = by_product["ssd_9560"]
    assert ssd["session_count"] == 2
    assert ssd["round_count"] == 3
    assert (ssd["valid"], ssd["invalid"], ssd["awaiting"], ssd["failed"], ssd["running"]) == (
        1,
        0,
        1,
        0,
        0,
    )
    assert ssd["valid_rate"] == 0.5
    assert body["root_cause_distribution"] == {
        "TEST_ENV_ERROR": 1,
        "FW_DEFECT": 1,
        "SCRIPT_ISSUE": 0,
        "HW_DEFECT": 0,
        "OTHER": 1,
    }
    knowledge = body["knowledge"]
    assert knowledge["case_count"] == 2
    assert knowledge["hot_memory_hit_rate"] == pytest.approx(0.75)
    assert knowledge["avg_confidence"] == pytest.approx((0.8 + 0.95 + 0.2) / 3)
    assert knowledge["low_confidence_ratio"] == pytest.approx(1 / 3)
    runtime = body["runtime"]
    assert runtime["queue_counts"] == {
        "PENDING": 2,
        "CLEANING": 1,
        "READY": 1,
        "AI_RUNNING": 1,
        "WAITING": 2,
    }
    assert runtime["paused_users"] == 1
    assert body["confidence_histogram"] == {
        "0.0-0.3": 1,
        "0.3-0.5": 0,
        "0.5-0.7": 0,
        "0.7-0.9": 1,
        "0.9-1.0": 1,
    }


def test_dashboard_summary_product_filter(test_settings: Settings, db_session: Session) -> None:
    _seed_dashboard_world(db_session)
    with TestClient(create_app(test_settings)) as client:
        response = client.get(
            "/api/v1/aitrace/dashboard/summary",
            params={"product_id": "ssd_9560"},
            headers=HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["product_id"] == "ssd_9560"
    overview = body["overview"]
    assert overview["session_count"] == 2
    assert overview["round_count"] == 3
    assert overview["valid_rate"] == pytest.approx(0.5)
    assert overview["round_distribution"] == {"1": 1, "2": 1, "3+": 0}
    assert overview["feedback_distribution"] == {
        "VALID": 1,
        "INVALID": 0,
        "AWAITING_FEEDBACK": 1,
        "FAILED": 0,
        "RUNNING": 0,
    }
    assert [row["product_id"] for row in body["by_product"]] == ["ssd_9560"]
    assert body["root_cause_distribution"] == {
        "TEST_ENV_ERROR": 1,
        "FW_DEFECT": 1,
        "SCRIPT_ISSUE": 0,
        "HW_DEFECT": 0,
        "OTHER": 0,
    }
    knowledge = body["knowledge"]
    assert knowledge["hot_memory_hit_rate"] == pytest.approx(1.0)
    assert knowledge["avg_confidence"] == pytest.approx((0.8 + 0.95) / 2)
    assert knowledge["low_confidence_ratio"] == 0.0
    assert body["confidence_histogram"] == {
        "0.0-0.3": 0,
        "0.3-0.5": 0,
        "0.5-0.7": 0,
        "0.7-0.9": 1,
        "0.9-1.0": 1,
    }


def test_dashboard_summary_empty_database(test_settings: Settings, db_session: Session) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/dashboard/summary", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["session_count"] == 0
    assert body["by_product"] == []
    assert body["knowledge"]["case_count"] == 0
    assert body["runtime"]["queue_counts"] == {
        "PENDING": 0,
        "CLEANING": 0,
        "READY": 0,
        "AI_RUNNING": 0,
        "WAITING": 0,
    }
