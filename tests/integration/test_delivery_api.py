from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from aitrace.api.app import create_app
from aitrace.core.settings import Settings

HEADERS = {"Authorization": "Bearer test-service-token"}


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "product_id": "ssd_9560",
        "batch_id": "BATCH-20261120-0089",
        "trigger_user_id": "zhangsan",
        "hardware_type": "SSD",
        "env_topology": {
            "host_ip": "10.12.8.45",
            "host_boot_time_utc": 1795104000,
            "devices": [
                {
                    "device_id": "D1",
                    "pcie_bdf": "0000:3b:00.0",
                    "device_node": "/dev/nvme0n1",
                    "model": "AliFlash V6 3.84T",
                    "sn": "SN-20240315-XXXX",
                    "fw_version": "8.1.2.3",
                    "slot": "SLOT-3",
                }
            ],
        },
        "git_context": {
            "repo_url": "https://gitlab.internal/ssd-qa/nvme-auto.git",
            "commit_id": "a9c4e12f",
        },
        "failed_cases": [
            {
                "case_id": "CASE-1021",
                "script_path": "tests/stress/test_io_reset.py::test_hot_reset",
                "failed_at_utc": 1795105200,
                "env_binding": {"device_id": "D1"},
                "logs": {"test_log_path": "/logs/test.log", "uart_log_path": "/logs/uart.log"},
            }
        ],
        "passed_cases": [{"case_id": "CASE-0987", "script_path": "tests/ok.py"}],
    }
    payload.update(overrides)
    return payload


def _isolate_tables(db_engine: Engine) -> None:
    # brief 测试直接使用 db_engine（无 db_session 清理），且重复投递用例依赖
    # 空表计数；为隔离跨测试残留，每个测试前清空交付涉及的全部表。
    with db_engine.begin() as conn:
        conn.execute(
            text(
                'TRUNCATE TABLE task_queue, user_queue, batch, report, "round", '
                '"session", profile RESTART IDENTITY CASCADE'
            )
        )


def test_delivery_creates_session_round_queue_and_batch(
    test_settings: Settings, db_engine: Engine
) -> None:
    _isolate_tables(db_engine)
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["batch_task_id"] == "T-BATCH-20261120-0089"
    assert body["sessions"][0]["case_id"] == "CASE-1021"
    assert body["sessions"][0]["status"] == "ACCEPTED"
    assert body["rejected"] == []

    with db_engine.connect() as conn:
        session = conn.execute(
            text(
                "SELECT domain_id, profile_version, trigger_user_id FROM session "
                "WHERE case_id='CASE-1021'"
            )
        ).one()
        assert session[0] == "enterprise"
        assert len(session[1]) == 16
        assert session[2] == "zhangsan"
        assert conn.execute(text('SELECT COUNT(*) FROM "round"')).scalar_one() == 1
        task = conn.execute(text("SELECT queue_state, task_type, queue_key FROM task_queue")).one()
        assert task == ("PENDING", "case_clean", "zhangsan")
        assert (
            conn.execute(
                text("SELECT in_flight FROM user_queue WHERE queue_key='zhangsan'")
            ).scalar_one()
            == 0
        )
        assert conn.execute(text("SELECT batch_state FROM batch")).scalar_one() == "RUNNING"


def test_duplicate_delivery_returns_existing_without_new_round(
    test_settings: Settings, db_engine: Engine
) -> None:
    _isolate_tables(db_engine)
    app = create_app(test_settings)
    with TestClient(app) as client:
        first = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
        second = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["sessions"][0]["is_existing"] is True
    assert second.json()["sessions"][0]["status"] == "EXISTING"
    with db_engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM "round"')).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM task_queue")).scalar_one() == 1


def test_missing_trigger_user_reports_chinese_field(
    test_settings: Settings,
) -> None:
    payload = _payload()
    del payload["trigger_user_id"]
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=payload, headers=HEADERS)
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PAYLOAD"
    assert "触发人" in body["message"]


def test_invalid_hardware_type_lists_allowed_values(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks", json=_payload(hardware_type="HDD"), headers=HEADERS
        )
    assert response.status_code == 400
    assert "合法取值" in response.json()["message"]
    assert "SSD" in response.json()["message"]


def test_unknown_device_binding_is_rejected(test_settings: Settings) -> None:
    payload = _payload()
    payload["failed_cases"][0]["env_binding"] = {"device_id": "D9"}  # type: ignore[index]
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=payload, headers=HEADERS)
    assert response.status_code == 400
    assert "设备清单" in response.json()["message"]


def test_payload_with_qoder_token_is_rejected(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks",
            json=_payload(qoder_access_token="forbidden"),
            headers=HEADERS,
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PAYLOAD"


def test_invalid_service_token_is_forbidden(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks",
            json=_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
