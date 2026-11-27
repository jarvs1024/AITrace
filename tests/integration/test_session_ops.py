# tests/integration/test_session_ops.py
import hashlib
import os

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine

from aitrace.api.app import create_app
from aitrace.core.settings import DatabaseSettings, RuntimeSettings, Settings, StorageSettings
from tests.integration.test_delivery_api import HEADERS, _payload

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
_DEFAULT_DSN = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"


def _isolate(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                'TRUNCATE TABLE task_queue, user_queue, batch, report, "round", "session", '
                "attachment, profile RESTART IDENTITY CASCADE"
            )
        )


def _storage_settings(tmp_path, *, max_attachment_bytes: int = 50 * 1024 * 1024) -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL or _DEFAULT_DSN),
        runtime=RuntimeSettings(enabled=False),
        storage=StorageSettings(data_dir=tmp_path, max_attachment_bytes=max_attachment_bytes),
    )


def _deliver(client: TestClient) -> str:
    response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202
    return response.json()["sessions"][0]["session_id"]


def _finish_latest_round(db_engine: Engine, session_id: str) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE \"round\" SET stage='COMPLETED', round_status='AWAITING_FEEDBACK' "
                "WHERE session_id=:sid"
            ),
            {"sid": session_id},
        )


def test_rediagnose_creates_new_round_with_hint_and_operator(
    test_settings: Settings, db_engine: Engine
) -> None:
    _isolate(db_engine)
    with TestClient(create_app(test_settings)) as client:
        session_id = _deliver(client)
        _finish_latest_round(db_engine, session_id)

        with db_engine.connect() as conn:
            response = client.post(
                f"/api/v1/aitrace/sessions/{session_id}/rediagnose",
                json={"trigger_user_id": "lisi", "hint": "怀疑是固件时序问题"},
                headers=HEADERS,
            )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == session_id
        assert body["round_no"] == 2
        assert body["round_id"].startswith("R-")
        assert body["task_id"].startswith("T-")

        with db_engine.connect() as conn:
            latest_round_no = conn.execute(
                text("SELECT latest_round_no FROM session WHERE session_id=:sid"),
                {"sid": session_id},
            ).scalar_one()
            assert latest_round_no == 2
            round_row = conn.execute(
                text(
                    'SELECT trigger_user_id, queue_key, round_status, stage FROM "round" '
                    "WHERE session_id=:sid AND round_no=2"
                ),
                {"sid": session_id},
            ).one()
            assert round_row[0] == "lisi"
            assert round_row[1] == "lisi"
            assert round_row[2] == "RUNNING"
            assert round_row[3] == "QUEUED"
            task = conn.execute(
                text(
                    "SELECT queue_key, queue_state, payload->>'hint', payload->>'logs' "
                    "FROM task_queue WHERE session_id=:sid AND round_no=2 "
                    "AND task_type='case_clean'"
                ),
                {"sid": session_id},
            ).one()
            assert task[0] == "lisi"
            assert task[1] == "PENDING"
            assert task[2] == "怀疑是固件时序问题"
            assert task[3] is not None  # 原清洗 payload（日志路径等）随轮次透传


def test_rediagnose_survives_in_place_clean_row_conversion(
    test_settings: Settings, db_engine: Engine
) -> None:
    # 首例路由会把 READY 的 case_clean 行原地转成 case_ai（router 既有模式），
    # 重诊必须仍能从该行取回清洗 payload（日志路径等），否则新轮次 NO_LOGS。
    _isolate(db_engine)
    with TestClient(create_app(test_settings)) as client:
        session_id = _deliver(client)
        _finish_latest_round(db_engine, session_id)
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE task_queue SET task_type='case_ai' WHERE session_id=:sid AND round_no=1"
                ),
                {"sid": session_id},
            )
        rediagnose = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/rediagnose",
            json={"trigger_user_id": "lisi", "hint": "结合新内核日志"},
            headers=HEADERS,
        )
        assert rediagnose.status_code == 200
        assert rediagnose.json()["round_no"] == 2
        with db_engine.connect() as conn:
            payload = conn.execute(
                text(
                    "SELECT payload->>'logs', payload->>'hint' FROM task_queue "
                    "WHERE session_id=:sid AND round_no=2 AND task_type='case_clean'"
                ),
                {"sid": session_id},
            ).one()
    assert payload[0] is not None  # 原清洗 payload（日志路径等）随轮次透传
    assert payload[1] == "结合新内核日志"


def test_rediagnose_running_session_returns_409(test_settings: Settings, db_engine: Engine) -> None:
    _isolate(db_engine)
    with TestClient(create_app(test_settings)) as client:
        session_id = _deliver(client)
        response = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/rediagnose",
            json={"trigger_user_id": "lisi"},
            headers=HEADERS,
        )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "SESSION_NOT_TERMINAL"
    assert "轮次" in body["message"]


def test_attachment_oversize_returns_413_chinese(
    test_settings: Settings, db_engine: Engine, tmp_path
) -> None:
    _isolate(db_engine)
    settings = _storage_settings(tmp_path, max_attachment_bytes=1024 * 1024)
    with TestClient(create_app(settings)) as client:
        session_id = _deliver(client)
        response = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/attachments",
            files={"file": ("oversize.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
            data={"uploaded_by": "zhangsan"},
            headers=HEADERS,
        )
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "ATTACHMENT_TOO_LARGE"
    assert "上限" in body["message"] and "字节" in body["message"]


def test_attachment_stores_file_sha256_and_row(
    test_settings: Settings, db_engine: Engine, tmp_path
) -> None:
    _isolate(db_engine)
    settings = _storage_settings(tmp_path)
    content = b"AITrace attachment log body\nsecond line\n"
    with TestClient(create_app(settings)) as client:
        session_id = _deliver(client)
        response = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/attachments",
            files={"file": ("uart.log", content, "text/plain")},
            data={"uploaded_by": "zhangsan"},
            headers=HEADERS,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["attachment_id"].startswith("A-")
    assert body["size_bytes"] == len(content)
    expected_digest = hashlib.sha256(content).hexdigest()
    assert body["sha256"] == expected_digest
    relative = body["path"]
    assert "rawlogs/BATCH-20261120-0089/uploads/" in relative

    disk = tmp_path / relative
    assert disk.read_bytes() == content
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT round_no, uploaded_by, sha256, path FROM attachment "
                "WHERE attachment_id=:aid"
            ),
            {"aid": body["attachment_id"]},
        ).one()
        assert row[0] == 2  # latest_round_no + 1：挂到下一次重诊轮次
        assert row[1] == "zhangsan"
        assert row[2] == expected_digest
        assert row[3] == relative


def test_attachment_filename_is_sanitized(
    test_settings: Settings, db_engine: Engine, tmp_path
) -> None:
    _isolate(db_engine)
    settings = _storage_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        session_id = _deliver(client)
        response = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/attachments",
            files={"file": ("../../escape.log", b"safe", "text/plain")},
            data={"uploaded_by": "zhangsan"},
            headers=HEADERS,
        )
    assert response.status_code == 200
    relative = response.json()["path"]
    assert "../" not in relative
    assert relative.endswith("escape.log")
    disk = tmp_path / relative
    assert disk.parent == tmp_path / "rawlogs" / "BATCH-20261120-0089" / "uploads"


def test_rediagnose_payload_carries_attach_paths(
    test_settings: Settings, db_engine: Engine, tmp_path
) -> None:
    _isolate(db_engine)
    settings = _storage_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        session_id = _deliver(client)
        upload = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/attachments",
            files={"file": ("evidence.txt", b"extra evidence", "text/plain")},
            data={"uploaded_by": "zhangsan"},
            headers=HEADERS,
        )
        assert upload.status_code == 200
        _finish_latest_round(db_engine, session_id)
        rediagnose = client.post(
            f"/api/v1/aitrace/sessions/{session_id}/rediagnose",
            json={"trigger_user_id": "zhangsan", "hint": "再看下电源时序"},
            headers=HEADERS,
        )
        assert rediagnose.status_code == 200
        with db_engine.connect() as conn:
            attach_paths = conn.execute(
                text(
                    "SELECT payload->'attach_paths' FROM task_queue "
                    "WHERE session_id=:sid AND round_no=2 AND task_type='case_clean'"
                ),
                {"sid": session_id},
            ).scalar_one()
    assert attach_paths == [upload.json()["path"]]


def test_session_detail_returns_rounds_referenced_logs_process_steps(
    test_settings: Settings, db_engine: Engine
) -> None:
    _isolate(db_engine)
    report_id = "RPT-DETAIL-1"
    with TestClient(create_app(test_settings)) as client:
        session_id = _deliver(client)
        _finish_latest_round(db_engine, session_id)
        with db_engine.begin() as conn:
            round_id = conn.execute(
                text('SELECT round_id FROM "round" WHERE session_id=:sid'),
                {"sid": session_id},
            ).scalar_one()
            conn.execute(
                text(
                    'UPDATE "round" SET report_id=:rid, confidence=0.85, finished_at=now() '
                    "WHERE round_id=:roid"
                ),
                {"rid": report_id, "roid": round_id},
            )
            conn.execute(
                text(
                    "INSERT INTO report (report_id, round_id, fault_info, result, "
                    "confidence_breakdown, evidence, referenced_logs, process_steps, "
                    "citation, gap_note, created_at) VALUES ("
                    ":rid, :roid, '{}', "
                    '\'{"root_cause":{"category":"FW_DEFECT","detail":"固件时序"}}\', '
                    "'[]', '[]', "
                    '\'[{"source_log":"uart","slice_path":"uart.log/s1",'
                    '"slice_hash":"h1"}]\', '
                    '\'[{"step":"定位首错","status":"done"}]\', NULL, NULL, now())'
                ),
                {"rid": report_id, "roid": round_id},
            )
        response = client.get(f"/api/v1/aitrace/sessions/{session_id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["round_no"] == 1
    rounds = body["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["round_no"] == 1
    assert rounds[0]["round_status"] == "AWAITING_FEEDBACK"
    assert rounds[0]["confidence"] == 0.85
    assert rounds[0]["report_id"] == report_id
    assert body["referenced_logs"] == [
        {"source_log": "uart", "slice_path": "uart.log/s1", "slice_hash": "h1"}
    ]
    assert body["process_steps"] == [{"step": "定位首错", "status": "done"}]
    assert body["grant_feedback"] is True
    assert body["report"] is not None
    assert body["report"]["report_id"] == report_id


def test_session_detail_grant_feedback_false_while_running(
    test_settings: Settings, db_engine: Engine
) -> None:
    _isolate(db_engine)
    with TestClient(create_app(test_settings)) as client:
        session_id = _deliver(client)
        response = client.get(f"/api/v1/aitrace/sessions/{session_id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["grant_feedback"] is False
    assert body["rounds"][0]["round_status"] == "RUNNING"
    assert body["referenced_logs"] == []
