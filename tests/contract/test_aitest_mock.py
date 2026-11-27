import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from aitest_mock import main as mock_main
from aitest_mock.main import app

READ = {"X-Aitest-Read-Token": "mock-read-token"}


def test_token_endpoint_returns_token_and_expiry() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/token",
            params={
                "product_id": "ssd_9560",
                "batch_id": "B1",
                "case_id": "C1",
                "trigger_user_id": "zhangsan",
                "action": "auto",
            },
            headers=READ,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["token"].startswith("mock-token-")
    assert body["expires_at"] > 0


def test_token_endpoint_serves_real_cn_pat_when_env_set(monkeypatch: Any) -> None:
    # cn 真实链验证：mock 作为真实 aitest 平台的替身，必须能下发用户托管的真实 CN 令牌
    monkeypatch.setenv("AITEST_MOCK_CN_PAT", "pt-real-cn-pat")
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/token",
            params={
                "product_id": "ssd_9560",
                "batch_id": "B1",
                "case_id": "C1",
                "trigger_user_id": "zhangsan",
                "action": "analyze",
            },
            headers=READ,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "pt-real-cn-pat"
    assert body["expires_at"] > 0


def test_token_scenarios_return_contract_errors() -> None:
    with TestClient(app) as client:
        missing = client.get(
            "/api/v1/aitest/token",
            params={
                "product_id": "p",
                "batch_id": "b",
                "case_id": "c",
                "trigger_user_id": "u",
                "action": "auto",
            },
            headers={**READ, "X-Mock-Scenario": "token_not_found"},
        )
        expired = client.get(
            "/api/v1/aitest/token",
            params={
                "product_id": "p",
                "batch_id": "b",
                "case_id": "c",
                "trigger_user_id": "u",
                "action": "auto",
            },
            headers={**READ, "X-Mock-Scenario": "token_expired"},
        )
    assert missing.status_code == 404
    assert missing.json()["code"] == "TOKEN_NOT_FOUND"
    assert expired.status_code == 410
    assert expired.json()["code"] == "TOKEN_EXPIRED"


def test_callback_receiver_records_progress() -> None:
    with TestClient(app) as client:
        client.post("/__mock/reset", headers=READ)
        response = client.post(
            "/api/v1/aitest/aitrace/status",
            json={"event": "PROGRESS", "session_id": "S1", "round_no": 1, "seq": 1},
            headers=READ,
        )
        callbacks = client.get("/__mock/callbacks", headers=READ).json()
    assert response.status_code == 200
    assert callbacks["items"][0]["session_id"] == "S1"


def test_read_token_is_required() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/cases", params={"batch_id": "B1"})
    assert response.status_code == 403


# --- 知因推送（deliver 代理）与状态轮询代理、演示页测试 ---


def _install_aitrace_stub(monkeypatch: Any, handler: Any) -> dict[str, Any]:
    """把 mock 的 httpx 客户端替换为 MockTransport，并固定知因地址与令牌环境变量。"""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content) if request.content else None
        return handler(request, captured)

    monkeypatch.setattr(mock_main, "_client", httpx.Client(transport=httpx.MockTransport(_handler)))
    monkeypatch.setenv("AITRACE_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "dev-service-token")
    return captured


def _delivery_response() -> dict[str, Any]:
    return {
        "accepted": True,
        "batch_task_id": "BT-1",
        "sessions": [
            {"case_id": "CASE-1021", "session_id": "S1", "is_existing": False, "status": "ACCEPTED"}
        ],
        "rejected": [],
    }


def test_deliver_forwards_with_bearer_and_passes_through(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(202, json=_delivery_response())

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/aitest/deliver", headers=READ)
    assert response.status_code == 202
    body = response.json()
    assert body["batch_task_id"] == "BT-1"
    assert body["sessions"][0]["session_id"] == "S1"
    assert captured["url"] == "http://127.0.0.1:8001/api/v1/aitrace/tasks"
    assert captured["authorization"] == "Bearer dev-service-token"
    sent = captured["body"]
    assert sent["batch_id"] == "BATCH-20261120-0089"
    assert sent["hardware_type"] == "SSD"
    assert [case["case_id"] for case in sent["failed_cases"]] == ["CASE-1021"]
    assert [case["case_id"] for case in sent["passed_cases"]] == ["CASE-0987"]


def test_deliver_payload_satisfies_aitrace_schema(monkeypatch: Any) -> None:
    from aitrace.api.schemas import DeliveryPayload

    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=_delivery_response())

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/aitest/deliver", headers=READ)
    assert response.status_code == 200
    DeliveryPayload.model_validate(captured["body"])


def test_deliver_filters_failed_cases(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=_delivery_response())

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        client.post("/api/v1/aitest/deliver", json={"failed_case_ids": ["CASE-1021"]}, headers=READ)
        rejected = client.post("/api/v1/aitest/deliver", json={"failed_case_ids": []}, headers=READ)
    assert [case["case_id"] for case in captured["body"]["failed_cases"]] == ["CASE-1021"]
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "NO_FAILED_CASES"


def test_deliver_returns_502_when_aitrace_unreachable(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/aitest/deliver", headers=READ)
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "AITRACE_UNREACHABLE"
    assert body["retryable"] is True
    assert "connection refused" in body["message"]
    assert "dev-service-token" not in json.dumps(body, ensure_ascii=False)


def test_status_proxy_passes_through(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "S1",
                "batch_id": "BATCH-20261120-0089",
                "case_id": "CASE-1021",
                "product_id": "ssd_9560",
                "hardware_type": "SSD",
                "domain_id": "domain-1",
                "profile_version": "v1",
                "round_no": 1,
                "stage": "COMPLETED",
                "progress": 100,
                "waiting_reason": None,
                "message": "知因分析完成",
                "report": None,
                "citation": None,
            },
        )

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/aitrace/status/S1", headers=READ)
    assert response.status_code == 200
    assert response.json()["stage"] == "COMPLETED"
    assert captured["url"] == "http://127.0.0.1:8001/api/v1/aitrace/tasks/S1"
    assert captured["authorization"] == "Bearer dev-service-token"


def test_status_proxy_reports_502_when_aitrace_unreachable(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/aitrace/status/S1", headers=READ)
    assert response.status_code == 502
    assert response.json()["code"] == "AITRACE_UNREACHABLE"


def test_index_serves_demo_page() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="deliver-btn"' in response.text


# --- 运营看板聚合（/__mock/stats）与投递记录、reset 清理 ---


def test_stats_aggregates_deliveries_callbacks_and_report(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(202, json=_delivery_response())

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        client.post("/__mock/reset", headers=READ)
        client.post("/api/v1/aitest/deliver", headers=READ)
        # 真实链路中 RESULT（优先级高）先于 PROGRESS 到达：看板必须按 seq 取最新阶段
        client.post(
            "/api/v1/aitest/aitrace/status",
            json={
                "event": "RESULT",
                "session_id": "S1",
                "seq": 2,
                "stage": "COMPLETED",
                "progress": 100,
                "report": {
                    "root_cause": {"category": "ENV", "detail": "环境配置错误", "evidence": []},
                    "solution": "修正拓扑",
                    "confidence": 0.9,
                    "gap_note": None,
                },
            },
            headers=READ,
        )
        client.post(
            "/api/v1/aitest/aitrace/status",
            json={
                "event": "PROGRESS",
                "session_id": "S1",
                "seq": 1,
                "stage": "AI_CLASSIFYING",
                "progress": 15,
            },
            headers=READ,
        )
        stats = client.get("/__mock/stats", headers=READ).json()
    assert stats["delivery_count"] == 1
    assert stats["session_count"] == 1
    assert stats["completed_count"] == 1
    assert stats["ai_completion_rate"] == 1.0
    assert stats["avg_confidence"] == 0.9
    assert stats["root_cause_distribution"] == {"ENV": 1}
    assert stats["event_distribution"] == {"PROGRESS": 1, "RESULT": 1}
    assert stats["progress_mean"] == 15
    recent = stats["recent_deliveries"]
    assert recent[0]["failed_case_ids"] == ["CASE-1021"]
    assert recent[0]["session_ids"] == ["S1"]
    assert recent[0]["trigger_user_id"] == "zhangsan"
    assert stats["sessions"][0]["stage"] == "COMPLETED"
    assert stats["sessions"][0]["confidence"] == 0.9
    assert stats["sessions"][0]["root_cause_category"] == "ENV"


def test_reset_clears_deliveries_and_callbacks(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(202, json=_delivery_response())

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        client.post("/__mock/reset", headers=READ)
        client.post("/api/v1/aitest/deliver", headers=READ)
        client.post(
            "/api/v1/aitest/aitrace/status",
            json={"event": "PROGRESS", "session_id": "S1"},
            headers=READ,
        )
        client.post("/__mock/reset", headers=READ)
        stats = client.get("/__mock/stats", headers=READ).json()
        callbacks = client.get("/__mock/callbacks", headers=READ).json()
    assert stats["delivery_count"] == 0
    assert stats["session_count"] == 0
    assert stats["completed_count"] == 0
    assert callbacks["items"] == []
