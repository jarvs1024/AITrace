from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aitrace.core.errors import AitraceError

_FIELD_LABELS = {
    "product_id": "产品 ID",
    "batch_id": "批次 ID",
    "trigger_user_id": "触发人",
    "hardware_type": "硬件类型",
    "env_topology": "环境拓扑",
    "git_context": "代码上下文",
    "failed_cases": "失败用例列表",
    "passed_cases": "通过用例列表",
    "case_id": "用例 ID",
    "script_path": "脚本路径",
    "failed_at_utc": "失败时间",
    "env_binding": "环境绑定",
    "logs": "日志路径",
    "devices": "设备清单",
    "host_ip": "主机 IP",
    "session_id": "会话 ID",
    "round_no": "轮次号",
    "reviewer": "评审人",
    "action": "评价动作",
    "correction": "修正信息",
    "root_cause_category": "根因分类",
    "reason": "修正说明",
    "hint": "诊断线索",
    "uploaded_by": "上传人",
}

_TOKEN_FIELD = re.compile(
    r"(^|[_.-])(token|secret|password|passwd|credential|authorization|api[_-]?key)($|[_.-])",
    re.IGNORECASE,
)

_REDACTED = "<已脱敏>"


def _is_token_field(field: str) -> bool:
    return _TOKEN_FIELD.search(field) is not None


def describe_validation(errors: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    parts: list[str] = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        field = ".".join(loc) if loc else "payload"
        leaf = loc[-1] if loc else "payload"
        label = _FIELD_LABELS.get(leaf, field)
        allowed = error.get("ctx", {}).get("expected")
        # 令牌红线：字段名含 token/secret 等一律不回显取值；模型级校验错误
        # 的 input 是整个请求体，同样不回显，避免任何令牌值进入响应或日志。
        sensitive = _is_token_field(field) or field == "payload"
        raw_input = error.get("input")
        shown_input = _REDACTED if sensitive else raw_input
        details.append(
            {
                "field": field,
                "label": label,
                "input": shown_input,
                "message": error.get("msg", ""),
                "allowed": allowed,
            }
        )
        text = f"{label} 当前值 {shown_input!r}，{error.get('msg', '')}"
        if allowed:
            text += f"；合法取值：{allowed}"
        parts.append(text)
    return "；".join(parts), details


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AitraceError)
    async def _aitrace_error(_: Request, exc: AitraceError) -> JSONResponse:
        body: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        }
        if exc.retry_after is not None:
            body["retry_after"] = exc.retry_after
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        message, details = describe_validation(list(exc.errors()))
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_PAYLOAD",
                "message": message,
                "retryable": False,
                "details": details,
            },
        )
