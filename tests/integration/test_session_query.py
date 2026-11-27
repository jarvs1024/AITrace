from fastapi.testclient import TestClient
from sqlalchemy import text

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from tests.integration.test_delivery_api import HEADERS, _payload

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, profile, memory_hot_file, case_contribution,
case_library, user_queue, task_queue, batch, report, "round", "session", callback_seq
RESTART IDENTITY CASCADE
"""


def test_query_returns_queued_stage(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        created = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS).json()
        session_id = created["sessions"][0]["session_id"]
        response = client.get(f"/api/v1/aitrace/tasks/{session_id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["stage"] == "QUEUED"
    assert body["progress"] == 10
    assert body["round_no"] == 1


def test_query_returns_failed_stage_when_ai_task_failed(
    test_settings: Settings, db_session
) -> None:
    with TestClient(create_app(test_settings)) as client:
        created = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS).json()
        session_id = created["sessions"][0]["session_id"]
        with db_session.bind.connect() as conn:  # type: ignore[union-attr]
            conn.execute(
                text(
                    "UPDATE task_queue SET queue_state='DONE' "
                    "WHERE session_id=:s AND task_type='case_clean'"
                ),
                {"s": session_id},
            )
            conn.execute(
                text(
                    "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no,"
                    " product_id, case_id, task_type, queue_key, queue_state, priority,"
                    " attempts, max_attempts, lease_generation, lease_until, callback_status,"
                    " payload, created_at, updated_at)"
                    " SELECT task_id || '-ai', batch_id, session_id, round_id, round_no,"
                    " product_id, case_id, 'case_ai', queue_key, 'FAILED', priority,"
                    " attempts, max_attempts, lease_generation, lease_until, callback_status,"
                    " '{}'::jsonb, created_at, updated_at"
                    " FROM task_queue WHERE session_id=:s AND task_type='case_clean'"
                ),
                {"s": session_id},
            )
            conn.execute(
                text("UPDATE task_queue SET error=:e WHERE session_id=:s AND task_type='case_ai'"),
                {"s": session_id, "e": '{"code": "TOKEN_NOT_FOUND", "message": "未托管令牌"}'},
            )
            conn.commit()
        response = client.get(f"/api/v1/aitrace/tasks/{session_id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "FAILED"
    assert body["progress"] is None
    # 还原全局隔离假设：本测试不依赖 db_session 的自启动截断，结束后主动清库，
    # 避免同文件内不使用 db_session 的用例（如 test_query_returns_queued_stage）读到残留行。
    with db_session.begin():
        db_session.execute(text(_TRUNCATE))


def test_query_unknown_session_returns_404(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/tasks/S-missing", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["code"] == "DELIVERY_NOT_FOUND"
