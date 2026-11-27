from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from aitrace.persistence.models import (
    CaseContribution,
    CaseLibrary,
    Report,
    Round,
    SessionModel,
    TaskQueue,
)
from tests.integration.test_delivery_api import HEADERS

DOMAIN = "domain-1"
NOW = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)


def _seed(
    db,
    *,
    sess_id: str = "S-fb-1",
    batch_id: str = "BATCH-FB",
    case_id: str = "CASE-FB",
    stable_fp: str | None = "fp-fb-abc",
    category: str = "FW_DEFECT",
    detail: str = "固件状态机死锁导致 IO 超时",
    confidence: float = 0.85,
    evidence: list[dict[str, Any]] | None = None,
    round_status: str = "AWAITING_FEEDBACK",
    citation: dict[str, Any] | None = None,
    report: bool = True,
) -> None:
    round_id = f"R-{sess_id}"
    report_id = f"RP-{sess_id}"
    ev = evidence or [{"excerpt": "u0: assert device state LOCKED"}]
    db.add(
        SessionModel(
            session_id=sess_id,
            product_id="ssd_9560",
            batch_id=batch_id,
            case_id=case_id,
            domain_id=DOMAIN,
            profile_version="v1",
            trigger_user_id="zhangsan",
            hardware_type="SSD",
            stable_fingerprint=stable_fp,
            env_topology={"host_ip": "10.0.0.1"},
            git_context={},
            latest_round_no=1,
            status="RUNNING",
            citation_group_id=None,
            created_at=NOW,
        )
    )
    # session → round → report 是纯表级外键链、无 ORM relationship，UoW 不保证
    # 跨 mapper 插入顺序，必须逐层 flush（与 test_release.py 等既有测试一致）。
    db.flush()
    db.add(
        Round(
            round_id=round_id,
            session_id=sess_id,
            round_no=1,
            trigger_user_id="zhangsan",
            queue_key="zhangsan",
            exact_fingerprint=f"exact-{sess_id}",
            stage="COMPLETED",
            round_status=round_status,
            report_id=report_id if report else None,
            confidence=confidence,
            feedback=None,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    db.flush()
    if report:
        db.add(
            Report(
                report_id=report_id,
                round_id=round_id,
                fault_info={"product": "ssd_9560", "symptom": detail},
                result={
                    "root_cause": {"category": category, "detail": detail, "evidence": ev},
                    "solution": "升级固件",
                    "confidence": confidence,
                    "gap_note": None,
                },
                confidence_breakdown=[],
                evidence=ev,
                referenced_logs=[],
                process_steps=[],
                citation=citation or {"citing_status": "NONE"},
                gap_note=None,
                created_at=NOW,
            )
        )
    db.commit()


def _post(
    client: TestClient,
    *,
    session_id: str,
    batch_id: str = "BATCH-FB",
    case_id: str = "CASE-FB",
    action: str,
    reviewer: str = "zhangsan",
    correction: dict[str, Any] | None = None,
) -> Any:
    response = client.post(
        "/api/v1/aitrace/feedback",
        json={
            "session_id": session_id,
            "batch_id": batch_id,
            "case_id": case_id,
            "reviewer": reviewer,
            "action": action,
            **({"correction": correction} if correction else {}),
        },
        headers=HEADERS,
    )
    return response


def test_valid_writes_contribution_and_projects_entry(test_settings: Settings, db_session) -> None:
    _seed(db_session)
    with TestClient(create_app(test_settings)) as client:
        response = _post(client, session_id="S-fb-1", action="VALID")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert body["round_no"] == 1
    assert body["version"] == 1
    assert body["feedback_id"].startswith("F-")
    contribution = db_session.execute(
        select(CaseContribution).where(CaseContribution.session_id == "S-fb-1")
    ).scalar_one()
    assert contribution.kind == "valid"
    assert contribution.status == "active"
    assert contribution.feedback_version == 1
    assert contribution.category == "FW_DEFECT"
    entry = db_session.execute(
        select(CaseLibrary).where(
            CaseLibrary.domain_id == DOMAIN, CaseLibrary.stable_fingerprint == "fp-fb-abc"
        )
    ).scalar_one()
    assert entry.category == "FW_DEFECT"
    assert entry.correction is None
    assert entry.confirm_count == 1
    assert entry.observation_hits == 0
    assert entry.entry_status == "valid"
    assert entry.exact_fingerprints == ["exact-S-fb-1"]
    assert "LOCKED" in entry.evidence_summary
    round_row = db_session.execute(select(Round).where(Round.session_id == "S-fb-1")).scalar_one()
    assert round_row.round_status == "VALID"
    assert round_row.feedback["version"] == 1
    materialize = db_session.execute(
        select(TaskQueue).where(
            TaskQueue.task_type == "memory_materialize", TaskQueue.queue_key == DOMAIN
        )
    ).scalar_one()
    assert materialize.queue_state == "PENDING"
    assert materialize.payload["domain_id"] == DOMAIN


def test_duplicate_same_reviewer_is_idempotent(test_settings: Settings, db_session) -> None:
    _seed(db_session)
    with TestClient(create_app(test_settings)) as client:
        first = _post(client, session_id="S-fb-1", action="VALID")
        dup = _post(client, session_id="S-fb-1", action="VALID")
    assert dup.status_code == 200
    assert dup.json()["status"] == "idempotent"
    assert dup.json()["version"] == first.json()["version"]
    contributions = (
        db_session.execute(select(CaseContribution).where(CaseContribution.session_id == "S-fb-1"))
        .scalars()
        .all()
    )
    assert len(contributions) == 1
    entry = db_session.execute(
        select(CaseLibrary).where(CaseLibrary.stable_fingerprint == "fp-fb-abc")
    ).scalar_one()
    assert entry.confirm_count == 1


def test_invalid_with_correction_writes_observing_lesson(
    test_settings: Settings, db_session
) -> None:
    _seed(db_session)
    with TestClient(create_app(test_settings)) as client:
        response = _post(
            client,
            session_id="S-fb-1",
            action="INVALID",
            correction={"root_cause_category": "TEST_ENV_ERROR", "reason": "PCIe 转接卡接触不良"},
        )
    assert response.status_code == 200
    contribution = db_session.execute(
        select(CaseContribution).where(CaseContribution.session_id == "S-fb-1")
    ).scalar_one()
    assert contribution.kind == "lesson"
    assert contribution.category == "TEST_ENV_ERROR"
    assert contribution.correction == "PCIe 转接卡接触不良"
    assert contribution.observation_hits == 0
    entry = db_session.execute(
        select(CaseLibrary).where(CaseLibrary.stable_fingerprint == "fp-fb-abc")
    ).scalar_one()
    assert entry.category == "TEST_ENV_ERROR"
    assert entry.correction == "PCIe 转接卡接触不良"
    assert entry.entry_status == "observing"
    assert entry.observation_hits == 0
    assert entry.confirm_count == 1
    round_row = db_session.execute(select(Round).where(Round.session_id == "S-fb-1")).scalar_one()
    assert round_row.round_status == "INVALID"


def test_version_change_withdraws_previous_and_recomputes(
    test_settings: Settings, db_session
) -> None:
    _seed(db_session)
    with TestClient(create_app(test_settings)) as client:
        _post(client, session_id="S-fb-1", action="VALID")
        changed = _post(
            client,
            session_id="S-fb-1",
            action="INVALID",
            correction={"root_cause_category": "TEST_ENV_ERROR", "reason": "转接卡松动"},
        )
        assert changed.json()["version"] == 2
        entry = db_session.execute(
            select(CaseLibrary).where(CaseLibrary.stable_fingerprint == "fp-fb-abc")
        ).scalar_one()
        assert entry.entry_status == "observing"
        assert entry.correction == "转接卡松动"
        stats = (
            db_session.execute(
                select(CaseContribution).where(CaseContribution.session_id == "S-fb-1")
            )
            .scalars()
            .all()
        )
        by_version = {c.feedback_version: c.status for c in stats}
        assert by_version == {1: "withdrawn", 2: "active"}
        final = _post(client, session_id="S-fb-1", action="VALID")
        assert final.json()["version"] == 3
    # API 用独立会话提交，需丢弃本地 identity map 缓存再读回最新状态
    db_session.expire_all()
    entry = db_session.execute(
        select(CaseLibrary).where(CaseLibrary.stable_fingerprint == "fp-fb-abc")
    ).scalar_one()
    assert entry.entry_status == "valid"
    assert entry.correction is None
    assert entry.category == "FW_DEFECT"
    assert entry.confirm_count == 1
    stats = (
        db_session.execute(
            select(CaseContribution)
            .where(CaseContribution.session_id == "S-fb-1")
            .order_by(CaseContribution.feedback_version)
        )
        .scalars()
        .all()
    )
    assert [c.status for c in stats] == ["withdrawn", "withdrawn", "active"]
    assert stats[2].kind == "valid"
    assert stats[2].feedback_version == 3


def test_invalid_without_correction_only_stats_no_memory(
    test_settings: Settings, db_session
) -> None:
    _seed(db_session)
    with TestClient(create_app(test_settings)) as client:
        response = _post(client, session_id="S-fb-1", action="INVALID")
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    contributions = (
        db_session.execute(select(CaseContribution).where(CaseContribution.session_id == "S-fb-1"))
        .scalars()
        .all()
    )
    assert contributions == []
    entries = db_session.execute(select(CaseLibrary)).scalars().all()
    assert entries == []
    materialize = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == "memory_materialize"))
        .scalars()
        .all()
    )
    assert materialize == []
    round_row = db_session.execute(select(Round).where(Round.session_id == "S-fb-1")).scalar_one()
    assert round_row.round_status == "INVALID"


def test_cascade_marks_citing_reports_stale_and_enqueues_notify(
    test_settings: Settings, db_session
) -> None:
    source_report_id = "RP-S-src"
    _seed(db_session, sess_id="S-src", batch_id="BATCH-FB", stable_fp="fp-src")
    _seed(
        db_session,
        sess_id="S-cite",
        batch_id="BATCH-FB",
        case_id="CASE-CITE",
        stable_fp="fp-src",
        citation={
            "source_session": "S-src",
            "source_round_no": 1,
            "source_report_id": source_report_id,
            "cited_version": 1,
            "citing_status": "CITING",
            "cascaded_from": None,
        },
    )
    with TestClient(create_app(test_settings)) as client:
        response = _post(
            client,
            session_id="S-src",
            action="INVALID",
            correction={"root_cause_category": "TEST_ENV_ERROR", "reason": "环境问题"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    citing_report = db_session.execute(
        select(Report).where(Report.round_id == "R-S-cite")
    ).scalar_one()
    assert citing_report.citation["citing_status"] == "CITING_STALE"
    assert citing_report.citation["source_session"] == "S-src"
    assert citing_report.citation["cascaded_from"] == {
        "source_session": "S-src",
        "source_round_no": 1,
        "source_report_id": source_report_id,
    }
    notify = db_session.execute(
        select(TaskQueue).where(TaskQueue.task_type == "notify")
    ).scalar_one()
    assert notify.queue_key == "BATCH-FB"
    assert notify.payload["event"] == "negated"
    assert notify.payload["session_id"] == "S-cite"


def test_valid_restores_stale_citing_reports(test_settings: Settings, db_session) -> None:
    _seed(db_session, sess_id="S-src", stable_fp="fp-src", case_id="CASE-SRC")
    _seed(
        db_session,
        sess_id="S-cite",
        case_id="CASE-CITE",
        stable_fp="fp-src",
        citation={
            "source_session": "S-src",
            "source_round_no": 1,
            "source_report_id": "RP-S-src",
            "cited_version": 1,
            "citing_status": "CITING_STALE",
            "cascaded_from": {
                "source_session": "S-src",
                "source_round_no": 1,
                "source_report_id": "RP-S-src",
            },
        },
    )
    with TestClient(create_app(test_settings)) as client:
        response = _post(client, session_id="S-src", case_id="CASE-SRC", action="VALID")
    assert response.status_code == 200
    citing_report = db_session.execute(
        select(Report).where(Report.round_id == "R-S-cite")
    ).scalar_one()
    assert citing_report.citation["citing_status"] == "CITING"
    assert citing_report.citation["source_session"] == "S-src"


def test_batch_common_session_skips_knowledge(test_settings: Settings, db_session) -> None:
    _seed(db_session, sess_id="S-common", case_id="", stable_fp=None)
    with TestClient(create_app(test_settings)) as client:
        response = _post(client, session_id="S-common", case_id="", action="VALID")
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    contributions = (
        db_session.execute(
            select(CaseContribution).where(CaseContribution.session_id == "S-common")
        )
        .scalars()
        .all()
    )
    assert contributions == []
    entries = db_session.execute(select(CaseLibrary)).scalars().all()
    assert entries == []
    round_row = db_session.execute(select(Round).where(Round.session_id == "S-common")).scalar_one()
    assert round_row.round_status == "VALID"


def test_conflicts_and_non_final_round_errors(test_settings: Settings, db_session) -> None:
    _seed(db_session)
    _seed(
        db_session,
        sess_id="S-running",
        stable_fp="fp-run",
        case_id="CASE-RUN",
        round_status="RUNNING",
    )
    with TestClient(create_app(test_settings)) as client:
        conflict = _post(client, session_id="S-fb-1", batch_id="BATCH-OTHER", action="VALID")
        running = _post(client, session_id="S-running", case_id="CASE-RUN", action="VALID")
        missing = _post(client, session_id="S-missing", action="VALID")
        invalid_category = _post(
            client,
            session_id="S-fb-1",
            action="INVALID",
            correction={"root_cause_category": "NOT_A_CATEGORY", "reason": "x"},
        )
    assert conflict.status_code == 400
    assert conflict.json()["code"] == "FEEDBACK_CONFLICT"
    assert "批次 ID" in conflict.json()["message"]
    assert running.status_code == 409
    assert running.json()["code"] == "FEEDBACK_ROUND_NOT_FINAL"
    assert missing.status_code == 404
    assert missing.json()["code"] == "DELIVERY_NOT_FOUND"
    assert invalid_category.status_code == 400
    assert "根因分类" in invalid_category.json()["message"]
    assert invalid_category.json()["code"] == "INVALID_PAYLOAD"


def test_valid_requires_report(test_settings: Settings, db_session) -> None:
    _seed(db_session, report=False, round_status="AWAITING_FEEDBACK")
    with TestClient(create_app(test_settings)) as client:
        response = _post(client, session_id="S-fb-1", action="VALID")
    assert response.status_code == 400
    assert response.json()["code"] == "FEEDBACK_NO_REPORT"
    assert "报告" in response.json()["message"]
