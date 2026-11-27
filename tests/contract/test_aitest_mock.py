import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
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
    assert [case["case_id"] for case in sent["failed_cases"]] == [
        "CASE-1021",
        "CASE-1048",
        "CASE-1035",
        "CASE-1042",
        "CASE-1055",
    ]
    assert [case["case_id"] for case in sent["passed_cases"]] == ["CASE-0987", "CASE-0988"]


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
    assert 'id="product-filter"' in response.text
    assert "/api/v1/aitest/aitrace/feedback" in response.text
    assert "/rediagnose" in response.text


# --- 六大看板、两态反馈与重诊代理（spec FR6/FR7/FR9：aitest 平台页同源呈现） ---


def test_dashboard_proxy_passes_product_filter(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"overview": {"session_count": 3}})

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        client.get("/api/v1/aitest/aitrace/dashboard", headers=READ)
        filtered = client.get(
            "/api/v1/aitest/aitrace/dashboard", params={"product_id": "ssd_9560"}, headers=READ
        )
    assert filtered.status_code == 200
    assert filtered.json()["overview"]["session_count"] == 3
    assert (
        captured["url"]
        == "http://127.0.0.1:8001/api/v1/aitrace/dashboard/summary?product_id=ssd_9560"
    )
    assert captured["authorization"] == "Bearer dev-service-token"


def test_dashboard_proxy_requires_read_token(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        raise AssertionError("令牌无效时不应触达知因")

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/aitrace/dashboard")
    assert response.status_code == 403


def test_feedback_proxy_forwards_body_and_bearer(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "feedback_id": "F1",
                "session_id": "S1",
                "round_no": 1,
                "version": 1,
                "status": "applied",
            },
        )

    captured = _install_aitrace_stub(monkeypatch, handler)
    body = {
        "session_id": "S1",
        "batch_id": "BATCH-20261120-0089",
        "case_id": "CASE-1021",
        "reviewer": "zhangsan",
        "action": "INVALID",
        "correction": {"root_cause_category": "TEST_ENV_ERROR", "reason": "环境拓扑不符"},
    }
    with TestClient(app) as client:
        response = client.post("/api/v1/aitest/aitrace/feedback", json=body, headers=READ)
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    assert captured["url"] == "http://127.0.0.1:8001/api/v1/aitrace/feedback"
    assert captured["authorization"] == "Bearer dev-service-token"
    assert captured["body"] == body


def test_feedback_proxy_requires_read_token(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        raise AssertionError("令牌无效时不应触达知因")

    _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/aitest/aitrace/feedback", json={"action": "VALID"})
    assert response.status_code == 403


def test_rediagnose_proxy_forwards_hint(monkeypatch: Any) -> None:
    def handler(request: httpx.Request, captured: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200, json={"session_id": "S1", "round_no": 2, "round_id": "R2", "task_id": "T2"}
        )

    captured = _install_aitrace_stub(monkeypatch, handler)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/aitest/aitrace/sessions/S1/rediagnose",
            json={"trigger_user_id": "zhangsan", "hint": "已补传内核日志"},
            headers=READ,
        )
    assert response.status_code == 200
    assert response.json()["round_no"] == 2
    assert captured["url"] == "http://127.0.0.1:8001/api/v1/aitrace/sessions/S1/rediagnose"
    assert captured["authorization"] == "Bearer dev-service-token"
    assert captured["body"]["hint"] == "已补传内核日志"


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
    assert recent[0]["failed_case_ids"] == [
        "CASE-1021",
        "CASE-1048",
        "CASE-1035",
        "CASE-1042",
        "CASE-1055",
    ]
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


# --- 日志清单 / 流式下载（spec §6.3）、批次环境、通知配置与 webhook 捕获 ---

_LOG_ROOT = Path(__file__).resolve().parents[2] / "mocks" / "aitest" / "logs"
_TEST_LOG = _LOG_ROOT / "var" / "log" / "aitest" / "CASE-1021" / "test.log"
_LOG_GENERATOR = (
    Path(__file__).resolve().parents[2] / "mocks" / "aitest" / "tools" / "generate_logs.py"
)
WEBHOOK_BASE = "http://127.0.0.1:8002"


@pytest.fixture(scope="module", autouse=True)
def _ensure_generated_logs() -> None:
    """mock 日志不入库（gitignore），缺失时用生成器重建，保证套件在新环境可跑。"""
    if not _TEST_LOG.is_file():
        subprocess.run(
            [sys.executable, str(_LOG_GENERATOR)], check=True, cwd=_LOG_GENERATOR.parents[1]
        )


def test_log_list_returns_case_logs_and_requires_token() -> None:
    with TestClient(app) as client:
        denied = client.get(
            "/api/v1/aitest/logs", params={"batch_id": "B1", "case_id": "CASE-1021"}
        )
        response = client.get(
            "/api/v1/aitest/logs",
            params={"batch_id": "BATCH-20261120-0089", "case_id": "CASE-1021"},
            headers=READ,
        )
    assert denied.status_code == 403
    assert response.status_code == 200
    items = response.json()["items"]
    paths = {item["path"] for item in items}
    assert "var/log/aitest/CASE-1021/test.log" in paths
    assert "var/log/aitest/CASE-1021/uart.log" in paths
    assert all(item["size_bytes"] > 0 for item in items)


def test_log_download_full_and_range() -> None:
    raw = _TEST_LOG.read_bytes()
    with TestClient(app) as client:
        full = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/test.log"},
            headers=READ,
        )
        part = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/test.log"},
            headers={**READ, "Range": "bytes=0-9"},
        )
        suffix = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/test.log"},
            headers={**READ, "Range": "bytes=-5"},
        )
        bad = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/test.log"},
            headers={**READ, "Range": "bytes=999999-"},
        )
    assert full.status_code == 200
    assert full.content == raw
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 0-9/{len(raw)}"
    assert part.content == raw[:10]
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == f"bytes {len(raw) - 5}-{len(raw) - 1}/{len(raw)}"
    assert suffix.content == raw[-5:]
    assert bad.status_code == 416
    assert bad.json()["code"] == "RANGE_NOT_SATISFIABLE"


def test_log_download_truncates_by_failed_at_utc() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/logs",
            params={
                "path": "var/log/aitest/CASE-TRUNC-BRACKET/test.log",
                "failed_at_utc": 1763100000,
            },
            headers=READ,
        )
    assert response.status_code == 200
    lines = response.content.decode("utf-8").splitlines()
    # 只保留失败时刻之前的行（按行对齐防追加写污染首错窗）
    assert len(lines) == 2
    assert lines[0].startswith("[1763099900]")
    assert lines[1].startswith("[1763100000]")


def test_log_download_truncates_iso_by_failed_at_utc() -> None:
    """ISO 时间戳（test.log）：失败时刻及之前的行保留，teardown/exit 截除。"""
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/test.log", "failed_at_utc": 1795148400},
            headers=READ,
        )
    assert response.status_code == 200
    lines = response.content.decode("utf-8").splitlines()
    assert lines[-1].startswith("2026-11-20T04:20:00.000Z TimeoutException: uart no response")
    assert any(
        line.startswith("2026-11-20T04:20:00.000Z Traceback (most recent call last):")
        for line in lines
    )
    assert not any("teardown begins" in line for line in lines)
    assert not any("exit code" in line for line in lines)


def test_log_download_truncates_uart_tick_by_failed_at_utc() -> None:
    """uart @TICK 相对毫秒：失败时刻前的 ASSERT 硬错行保留，失败后的 post-mortem 行截除。"""
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/uart.log", "failed_at_utc": 1795148400},
            headers=READ,
        )
    assert response.status_code == 200
    lines = response.content.decode("utf-8").splitlines()
    assert any("ASSERT" in line for line in lines)
    assert not any("post-mortem" in line for line in lines)
    assert not any("awaiting host command" in line for line in lines)


def test_log_download_truncates_dmesg_rel_by_failed_at_utc() -> None:
    """dmesg 相对秒（开机基准）：失败时刻前的 BUG/Call Trace 保留，失败后的 device offline 截除。"""
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1042/dmesg.log", "failed_at_utc": 1795172400},
            headers=READ,
        )
    assert response.status_code == 200
    lines = response.content.decode("utf-8").splitlines()
    assert any("BUG: kernel NULL pointer dereference" in line for line in lines)
    assert any("Call Trace:" in line for line in lines)
    assert not any("device offline" in line for line in lines)


def test_log_download_truncates_keeps_unparsable_lines() -> None:
    """无法识别时间戳的行保守保留（banner 等无时间戳内容不被误删）。"""
    raw_head = (
        (_LOG_ROOT / "var" / "log" / "aitest" / "CASE-1021" / "uart.log")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/logs",
            params={"path": "var/log/aitest/CASE-1021/uart.log", "failed_at_utc": 1795148400},
            headers=READ,
        )
    assert response.status_code == 200
    assert response.content.decode("utf-8").splitlines()[0] == raw_head


def test_log_download_safety_and_missing() -> None:
    with TestClient(app) as client:
        traversal = client.get(
            "/api/v1/aitest/logs",
            params={"path": "../../deploy/secrets/README.md"},
            headers=READ,
        )
        absolute = client.get("/api/v1/aitest/logs", params={"path": "/etc/passwd"}, headers=READ)
        missing = client.get(
            "/api/v1/aitest/logs", params={"path": "var/log/aitest/nope.log"}, headers=READ
        )
    assert traversal.status_code == 403
    assert traversal.json()["code"] == "PATH_FORBIDDEN"
    assert absolute.status_code == 403
    assert absolute.json()["code"] == "PATH_FORBIDDEN"
    assert missing.status_code == 404
    assert missing.json()["code"] == "LOG_NOT_FOUND"


def test_batch_env_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/tasks/BATCH-20261120-0089", headers=READ)
        missing = client.get("/api/v1/aitest/tasks/BATCH-NOPE", headers=READ)
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == "BATCH-20261120-0089"
    assert body["env_topology"]["host_ip"] == "10.23.45.67"
    assert body["git_context"]["commit_id"].startswith("abc123")
    assert missing.status_code == 404
    assert missing.json()["code"] == "BATCH_NOT_FOUND"


def test_notify_config_and_webhook_capture(monkeypatch: Any) -> None:
    monkeypatch.setenv("AITEST_MOCK_WEBHOOK_BASE", WEBHOOK_BASE)
    with TestClient(app) as client:
        client.post("/__mock/reset", headers=READ)
        config = client.get(
            "/api/v1/aitest/notify-config", params={"product_id": "ssd_9560"}, headers=READ
        )
        post = client.post(
            "/api/v1/aitest/notify/webhook",
            json={"msgtype": "text", "text": {"content": "聚合卡片"}},
        )
        cards = client.get("/__mock/cards", headers=READ)
        client.post("/__mock/reset", headers=READ)
        cleared = client.get("/__mock/cards", headers=READ)
    assert config.status_code == 200
    body = config.json()
    assert body["webhook"] == f"{WEBHOOK_BASE}/api/v1/aitest/notify/webhook"
    assert body["high_risk_webhook"] is None
    assert "zhangsan" in body["at_users"]
    assert post.status_code == 200
    assert cards.json()["items"][-1]["text"]["content"] == "聚合卡片"
    assert cleared.json()["items"] == []
