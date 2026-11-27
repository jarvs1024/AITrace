# tests/integration/test_monitor_api.py
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from aitrace.persistence.models import TaskQueue, UserQueue
from tests.integration.test_delivery_api import HEADERS


def test_monitor_endpoint_guarded(test_settings: Settings, db_session: Session) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/monitor")
    assert response.status_code in (401, 403)


def test_monitor_endpoint_with_service_token(test_settings: Settings, db_session: Session) -> None:
    now = datetime.now(UTC)
    db_session.add(
        TaskQueue(
            task_id="T-MON-1",
            batch_id="B-MON-1",
            session_id=None,
            round_id="R-MON-1",
            round_no=0,
            product_id="ssd_9560",
            case_id="",
            task_type="case_ai",
            queue_key="zhangsan",
            queue_state="READY",
            created_at=now - timedelta(seconds=130),
            updated_at=now - timedelta(seconds=130),
        )
    )
    db_session.add(UserQueue(queue_key="zhangsan", state="ACTIVE", updated_at=now))
    db_session.commit()
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/monitor", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert "waits" in body
    assert "watermark" in body
    assert body["watermark"]["level"] in ("OK", "WARN", "FORCE")
    assert body["audit"]["verified"] in (True, False)
    assert "callback" not in body["generated_at"]  # generated_at 是 ISO 时间戳
    waits_blob = str(body["waits"])
    assert "zhangsan" in waits_blob


def test_dashboard_summary_includes_monitoring_section(
    test_settings: Settings, db_session: Session
) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/dashboard/summary", headers=HEADERS)
    assert response.status_code == 200
    monitoring = response.json()["monitoring"]
    assert monitoring is not None
    for key in ("generated_at", "runtime", "waits", "fairness", "alerts_eligible", "watermark"):
        assert key in monitoring
    assert monitoring["watermark"]["level"] in ("OK", "WARN", "FORCE")


def test_monitor_endpoint_empty_database(test_settings: Settings, db_session: Session) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/monitor", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["paused_auth"]["count"] == 0
    assert body["workers"]["ai_running"] == 0
