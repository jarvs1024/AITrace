from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import yaml
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="aitest mock", version="0.1.0")

_READ_TOKEN = os.getenv("AITEST_READ_TOKEN", "mock-read-token")
_SCENARIO = Path(__file__).resolve().parents[2] / "scenarios" / "default.yaml"
_LOGS_ROOT = Path(__file__).resolve().parents[2] / "logs"
_INDEX_HTML = Path(__file__).resolve().parent / "index.html"
_WEBHOOK_BASE = os.getenv("AITEST_MOCK_WEBHOOK_BASE", "http://127.0.0.1:8002")
_callbacks: list[dict[str, Any]] = []
_deliveries: list[dict[str, Any]] = []
_cards: list[dict[str, Any]] = []
_client: httpx.Client | None = None
# 运行时令牌场景覆盖（默认 None=不覆盖）；供 e2e 令牌异常路径动态切换。
_scenario_override: str | None = None


class MockApiError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@app.exception_handler(MockApiError)
async def _mock_error(_: Request, exc: MockApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def _auth(token: str | None) -> None:
    if token != _READ_TOKEN:
        raise MockApiError(403, "FORBIDDEN", "读取令牌无效")


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        # 知因服务走内网直连：不读取环境代理（环境中的 *_proxy 变量可能是畸形值，
        # 会令 httpx 0.28 在构建客户端时抛 InvalidURL）。
        _client = httpx.Client(timeout=10.0, trust_env=False)
    return _client


def _forward(method: str, path: str, json_body: dict[str, Any] | None = None) -> httpx.Response:
    """把请求转发给知因（Bearer 服务令牌只放在请求头，绝不进入错误消息）。"""
    base_url = os.getenv("AITRACE_BASE_URL", "http://127.0.0.1:8001")
    token = os.getenv("AITRACE_SERVICE_TOKEN", "dev-service-token")
    try:
        return _get_client().request(
            method,
            f"{base_url}{path}",
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise MockApiError(
            502, "AITRACE_UNREACHABLE", f"知因服务不可达：{exc}", retryable=True
        ) from exc


def _json_passthrough(response: httpx.Response) -> JSONResponse:
    try:
        body = response.json()
    except ValueError:
        raise MockApiError(502, "AITRACE_BAD_RESPONSE", "知因服务返回了非 JSON 响应") from None
    return JSONResponse(status_code=response.status_code, content=body)


def _build_delivery_payload(
    data: dict[str, Any], failed_ids: list[str] | None, *, batch_id: str
) -> dict[str, Any]:
    failed_cases: list[dict[str, Any]] = []
    passed_cases: list[dict[str, Any]] = []
    for case in data["cases"]:
        if case["status"] == "FAILED":
            if failed_ids is None or case["case_id"] in failed_ids:
                failed_cases.append(
                    {
                        "case_id": case["case_id"],
                        "script_path": case["script_path"],
                        "failed_at_utc": case["failed_at_utc"],
                        "env_binding": case["env_binding"],
                        "logs": case["logs"],
                    }
                )
        else:
            passed_cases.append({"case_id": case["case_id"], "script_path": case["script_path"]})
    return {
        "product_id": data["product_id"],
        "batch_id": batch_id,
        "trigger_user_id": data["trigger_user_id"],
        "hardware_type": data["hardware_type"],
        "env_topology": data["env_topology"],
        "git_context": data["git_context"],
        "failed_cases": failed_cases,
        "passed_cases": passed_cases,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/aitest/token")
def token(
    product_id: str,
    batch_id: str,
    case_id: str,
    trigger_user_id: str,
    action: str,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
    x_mock_scenario: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    scenario = x_mock_scenario or _scenario_override
    if scenario == "token_not_found":
        raise MockApiError(404, "TOKEN_NOT_FOUND", "用户未托管令牌")
    if scenario == "token_expired":
        raise MockApiError(410, "TOKEN_EXPIRED", "令牌已过期")
    # cn 真实链验证：mock 作为真实 aitest 平台的替身，可通过环境变量下发用户托管的真实 CN PAT
    # （仅驻留运行进程内存，响应走回环 HTTP，绝不落库或写日志）
    real_pat = os.getenv("AITEST_MOCK_CN_PAT")
    return {
        "token": real_pat or f"mock-token-{trigger_user_id}",
        "expires_at": int(time.time()) + 3600,
        "binding": {
            "product_id": product_id,
            "batch_id": batch_id,
            "case_id": case_id,
            "action": action,
        },
    }


@app.get("/api/v1/aitest/cases")
def cases(
    batch_id: str = Query(...),
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    data = yaml.safe_load(_SCENARIO.read_text(encoding="utf-8"))
    return {"batch_id": batch_id, "cases": data["cases"]}


@app.get("/api/v1/aitest/tasks/{batch_id}")
def batch_env(
    batch_id: str,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """批次任务环境信息（对账核验设备映射与补全批次上下文）。"""
    _auth(x_aitest_read_token)
    data = yaml.safe_load(_SCENARIO.read_text(encoding="utf-8"))
    if data.get("batch_id") != batch_id:
        raise MockApiError(404, "BATCH_NOT_FOUND", "批次不存在")
    return {
        "batch_id": batch_id,
        "env_topology": data.get("env_topology", {}),
        "git_context": data.get("git_context", {}),
    }


def _resolve_log_path(path: str) -> Path:
    """把 path 解析到日志根目录（白名单）；拒绝越界 / 反斜杠穿越 / 绝对路径。"""
    if path.startswith("/") or "/../" in path or ".." in Path(path).parts:
        raise MockApiError(403, "PATH_FORBIDDEN", "日志路径越界")
    candidate = (_LOGS_ROOT / path).resolve()
    if candidate != _LOGS_ROOT and _LOGS_ROOT not in candidate.parents:
        raise MockApiError(403, "PATH_FORBIDDEN", "日志路径越界")
    return candidate


_TIMESTAMP_RE = re.compile(r"^\[(\d+)\]")


def _truncate_by_failed_at(data: bytes, failed_at_utc: int) -> bytes:
    """按失败时刻逻辑截断（按行对齐）：只保留时间戳 <= failed_at_utc 的行；无时间戳行保守保留。"""
    kept: list[bytes] = []
    for line in data.splitlines():
        match = _TIMESTAMP_RE.match(line.decode("utf-8", errors="replace"))
        if match is None or int(match.group(1)) <= failed_at_utc:
            kept.append(line)
    return b"\n".join(kept)


def _parse_range(value: str, size: int) -> tuple[int, int]:
    spec = value.removeprefix("bytes=").split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    if start_s == "":
        length = int(end_s)
        start, end = max(size - length, 0), size - 1
    elif end_s == "":
        start, end = int(start_s), size - 1
    else:
        start, end = int(start_s), int(end_s)
    if start >= size or start > end:
        raise MockApiError(416, "RANGE_NOT_SATISFIABLE", "分段范围越界")
    return start, min(end, size - 1)


@app.get("/api/v1/aitest/logs")
def log_download(
    request: Request,
    path: str | None = None,
    failed_at_utc: int | None = None,
    batch_id: str | None = None,
    case_id: str | None = None,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> Response:
    """日志清单（batch_id+case_id）或单文件流式下载（path，支持 Range / failed_at_utc 截断）。"""
    _auth(x_aitest_read_token)
    if path is None:
        if batch_id is None or case_id is None:
            raise MockApiError(400, "MISSING_FILTER", "日志清单需要 batch_id 与 case_id")
        prefix = _resolve_log_path(f"var/log/aitest/{case_id}")
        if not prefix.is_dir():
            return JSONResponse(status_code=200, content={"batch_id": batch_id, "items": []})
        items: list[dict[str, Any]] = []
        for file in sorted(prefix.rglob("*")):
            if file.is_file():
                items.append(
                    {
                        "path": str(file.relative_to(_LOGS_ROOT)),
                        "size_bytes": file.stat().st_size,
                    }
                )
        return JSONResponse(status_code=200, content={"batch_id": batch_id, "items": items})
    candidate = _resolve_log_path(path)
    if not candidate.is_file():
        raise MockApiError(404, "LOG_NOT_FOUND", "日志不存在")
    data = candidate.read_bytes()
    if failed_at_utc is not None:
        data = _truncate_by_failed_at(data, failed_at_utc)
    headers = {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        start, end = _parse_range(range_header, len(data))
        return Response(
            content=data[start : end + 1],
            status_code=206,
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"},
        )
    return Response(content=data, status_code=200, headers=headers)


@app.get("/api/v1/aitest/notify-config")
def notify_config(
    product_id: str,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """分产品通知配置（钉钉机器人 webhook + @人名单 + 高危独立群）。"""
    _auth(x_aitest_read_token)
    return {
        "webhook": f"{_WEBHOOK_BASE}/api/v1/aitest/notify/webhook",
        "at_users": ["zhangsan"],
        "high_risk_webhook": None,
    }


@app.post("/api/v1/aitest/notify/webhook")
def notify_webhook(payload: dict[str, Any]) -> dict[str, bool]:
    """钉钉机器人 webhook 替身：捕获知因发来的聚合卡片，供运营核对与测试断言。"""
    _cards.append(payload)
    return {"ok": True}


@app.get("/__mock/cards")
def cards(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    return {"items": list(_cards)}


@app.post("/api/v1/aitest/deliver")
def deliver(
    payload: dict[str, Any] | None = None,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """把场景中的失败用例推送给知因，透传知因的响应与状态码。"""
    _auth(x_aitest_read_token)
    data = yaml.safe_load(_SCENARIO.read_text(encoding="utf-8"))
    failed_ids = payload.get("failed_case_ids") if payload else None
    batch_id = (payload or {}).get("batch_id") or data["batch_id"]
    delivery = _build_delivery_payload(data, failed_ids, batch_id=batch_id)
    if not delivery["failed_cases"]:
        raise MockApiError(400, "NO_FAILED_CASES", "没有可推送的失败用例")
    response = _forward("POST", "/api/v1/aitrace/tasks", delivery)
    _deliveries.append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "batch_id": batch_id,
            "trigger_user_id": delivery["trigger_user_id"],
            "failed_case_ids": [c["case_id"] for c in delivery["failed_cases"]],
            "http_status": response.status_code,
            "sessions": _session_refs_from_response(response),
        }
    )
    return _json_passthrough(response)


def _session_refs_from_response(response: httpx.Response) -> list[dict[str, str]]:
    try:
        body = response.json()
    except ValueError:
        return []
    sessions = body.get("sessions") if isinstance(body, dict) else None
    if not isinstance(sessions, list):
        return []
    return [
        {"case_id": s["case_id"], "session_id": s["session_id"]}
        for s in sessions
        if isinstance(s, dict) and s.get("case_id") and s.get("session_id")
    ]


@app.get("/api/v1/aitest/aitrace/status/{session_id}")
def status_proxy(
    session_id: str,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """把会话状态查询透传给知因。"""
    _auth(x_aitest_read_token)
    return _json_passthrough(_forward("GET", f"/api/v1/aitrace/tasks/{session_id}"))


@app.post("/api/v1/aitest/aitrace/status")
def receive_status(
    payload: dict[str, Any],
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    _auth(x_aitest_read_token)
    _callbacks.append(payload)
    return {"ok": True}


@app.get("/api/v1/aitest/aitrace/dashboard")
def dashboard_proxy(
    product_id: str | None = None,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """六大运营视图数据（spec FR7）：aitest 平台页同源呈现，只拉不推。"""
    _auth(x_aitest_read_token)
    path = "/api/v1/aitrace/dashboard/summary"
    if product_id:
        path += f"?product_id={product_id}"
    return _json_passthrough(_forward("GET", path))


@app.post("/api/v1/aitest/aitrace/feedback")
def feedback_proxy(
    payload: dict[str, Any],
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """两态反馈代理（spec FR6）：VALID / INVALID±修正，透传知因响应。"""
    _auth(x_aitest_read_token)
    return _json_passthrough(_forward("POST", "/api/v1/aitrace/feedback", payload))


@app.post("/api/v1/aitest/aitrace/sessions/{session_id}/rediagnose")
def rediagnose_proxy(
    session_id: str,
    payload: dict[str, Any] | None = None,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """重诊代理（spec FR9）：追加轮次，透传知因响应。"""
    _auth(x_aitest_read_token)
    body = payload or {}
    return _json_passthrough(
        _forward(
            "POST",
            f"/api/v1/aitrace/sessions/{session_id}/rediagnose",
            {
                "trigger_user_id": body.get("trigger_user_id") or "zhangsan",
                "hint": body.get("hint"),
            },
        )
    )


@app.get("/__mock/callbacks")
def callbacks(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    return {"items": list(_callbacks)}


@app.get("/__mock/stats")
def stats(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """运营看板聚合：投递/会话/回调三方数据汇总。"""
    _auth(x_aitest_read_token)
    session_states: dict[str, dict[str, Any]] = {}
    for cb in _callbacks:
        sid = cb.get("session_id")
        if not sid:
            continue
        state = session_states.setdefault(sid, {"session_id": sid, "stage": None, "seq": -1})
        # 回调投递不保证顺序（RESULT 优先级高于 PROGRESS）：以 seq 最大者作为会话当前阶段
        seq = int(cb.get("seq") or 0)
        if cb.get("stage") and seq >= state["seq"]:
            state["seq"] = seq
            state["stage"] = cb["stage"]
        if cb.get("event") == "RESULT":
            report = cb.get("report") or {}
            state["confidence"] = report.get("confidence")
            rc = report.get("root_cause") or {}
            state["root_cause_category"] = rc.get("category")
    session_list = list(session_states.values())
    completed = [s for s in session_list if s["stage"] == "COMPLETED"]
    result_events = [cb for cb in _callbacks if cb.get("event") == "RESULT"]
    confidences = [
        float(r.get("report", {}).get("confidence"))
        for r in result_events
        if isinstance(r.get("report", {}).get("confidence"), (int, float))
    ]
    root_cause_distribution: dict[str, int] = {}
    for s in session_list:
        cat = s.get("root_cause_category")
        if cat:
            root_cause_distribution[cat] = root_cause_distribution.get(cat, 0) + 1
    event_distribution: dict[str, int] = {}
    for cb in _callbacks:
        ev = cb.get("event")
        if ev:
            event_distribution[ev] = event_distribution.get(ev, 0) + 1
    progress_values = [
        int(p.get("progress") or 0)
        for p in _callbacks
        if p.get("event") == "PROGRESS" and isinstance(p.get("progress"), (int, float))
    ]
    progress_total = sum(progress_values)
    return {
        "delivery_count": len(_deliveries),
        "session_count": len(session_list),
        "completed_count": len(completed),
        "ai_completion_rate": (len(completed) / len(session_list)) if session_list else None,
        "avg_confidence": (sum(confidences) / len(confidences)) if confidences else None,
        "root_cause_distribution": root_cause_distribution,
        "event_distribution": event_distribution,
        "progress_mean": (progress_total / len(progress_values)) if progress_values else None,
        "recent_deliveries": [
            {
                "at": d["at"],
                "batch_id": d["batch_id"],
                "trigger_user_id": d["trigger_user_id"],
                "failed_case_ids": d["failed_case_ids"],
                "session_ids": [s["session_id"] for s in d["sessions"]],
            }
            for d in _deliveries[-10:]
        ],
        "sessions": session_list,
    }


@app.post("/__mock/scenario")
def scenario_override(
    payload: dict[str, Any],
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """运行时覆盖令牌场景（token_scenario=None 恢复默认），供 e2e 令牌异常路径使用。"""
    _auth(x_aitest_read_token)
    global _scenario_override
    _scenario_override = payload.get("token_scenario")
    return {"ok": True}


@app.post("/__mock/reset")
def reset(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, bool]:
    _auth(x_aitest_read_token)
    global _scenario_override
    _scenario_override = None
    _callbacks.clear()
    _deliveries.clear()
    _cards.clear()
    return {"ok": True}
