# src/aitrace/ai/qoder.py
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aitrace.core.settings import AISettings


class TokenExpiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class QoderEvent:
    kind: Literal["delta", "subagent", "result"]
    text: str
    data: dict[str, Any]


_EXPIRED_NEEDLES = ("expired", "过期", "TOKEN_EXPIRED", "authentication_failed")
_INVALID_NEEDLES = ("invalid", "无效", "TOKEN_INVALID")
_TIMEOUT_NEEDLES = ("timeout", "超时")
_RATE_NEEDLES = ("rate_limit", "rate limit", "限流", "TOKEN_RATE_LIMITED")
_HTTP_5XX_NEEDLES = ("http 5", " 5xx", "502 ", "503 ", "504 ")


def classify_sdk_exception(exc: BaseException) -> dict[str, str]:
    description = (str(exc) or type(exc).__name__)[:200]
    lowered = description.lower()
    if any(n.lower() in lowered for n in _EXPIRED_NEEDLES):
        code = "TOKEN_EXPIRED"
    elif any(n.lower() in lowered for n in _INVALID_NEEDLES):
        code = "TOKEN_INVALID"
    elif any(n.lower() in lowered for n in _RATE_NEEDLES):
        code = "TOKEN_RATE_LIMITED"
    elif any(n.lower() in lowered for n in _TIMEOUT_NEEDLES):
        code = "TIMEOUT"
    elif any(n.lower() in lowered for n in _HTTP_5XX_NEEDLES):
        code = "HTTP_5XX"
    else:
        code = "SDK_ERROR"
    return {"code": code, "description": description}


def _raise_sdk_error(description: str) -> None:
    classified = classify_sdk_exception(RuntimeError(description))
    if classified["code"] == "TOKEN_EXPIRED":
        raise TokenExpiredError(classified["description"])
    raise RuntimeError(f"Qoder SDK 调用失败：{classified['code']} {classified['description']}")


class CnSdkAdapter:
    def __init__(self, settings: AISettings) -> None:
        self._settings = settings

    def _load_sdk(self) -> Any:
        try:
            if self._settings.edition == "intl":
                import qoder_agent_sdk as sdk  # type: ignore[import-not-found]
            else:
                import qodercn_agent_sdk as sdk
        except ImportError as exc:
            raise RuntimeError(
                f"Qoder {self._settings.edition} SDK 未安装：请执行 pip install "
                f"qoder{'c' if self._settings.edition == 'cn' else ''}n-agent-sdk"
            ) from exc
        return sdk

    async def stream(
        self,
        token: str,
        prompt: str,
        *,
        cwd: Path | None,
        on_auth_expired: Callable[[], None] | None = None,
        subagents: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[QoderEvent]:
        sdk = self._load_sdk()
        opts = sdk.QoderAgentOptions()
        opts.auth = sdk.access_token(token)
        opts.model = self._settings.model
        opts.vpc_endpoint = self._settings.vpc_endpoint
        opts.control_request_timeout_ms = self._settings.sdk_control_timeout_ms
        opts.max_turns = self._settings.max_turns
        opts.permission_mode = "dontAsk"
        opts.include_partial_messages = True
        opts.disallowed_tools = ["Bash", "Write", "Edit"]
        if cwd is not None:
            opts.cwd = str(cwd)
        if subagents:
            opts.agents = _build_agents(sdk, subagents)
        if on_auth_expired is not None:
            opts.on_auth_expired = on_auth_expired
        try:
            async for msg in sdk.query(prompt=prompt, options=opts):
                event = _map_sdk_message(msg)
                if event is not None:
                    yield event
        except TokenExpiredError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            classified = classify_sdk_exception(exc)
            if classified["code"] == "TOKEN_EXPIRED":
                raise TokenExpiredError(classified["description"]) from exc
            raise RuntimeError(
                f"Qoder SDK 调用失败：{classified['code']} {classified['description']}"
            ) from exc


def _build_agents(sdk: Any, subagents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """子代理配置序列转 SDK 的 dict[str, AgentDefinition]；name 作字典键。"""
    agents: dict[str, Any] = {}
    for item in subagents:
        name = item.get("name")
        if not name:
            raise RuntimeError("子代理配置缺失必填项 name；要求：每个子代理配置包含 name 键")
        agents[name] = sdk.AgentDefinition(**{k: v for k, v in item.items() if k != "name"})
    return agents


def _map_sdk_message(msg: Any) -> QoderEvent | None:
    cls_name = type(msg).__name__
    if cls_name == "StreamEvent":
        event = getattr(msg, "event", None) or {}
        if event.get("type") != "content_block_delta":
            return None
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return None  # thinking_delta 不进入正文累积
        return QoderEvent("delta", str(delta.get("text") or ""), {})
    if cls_name == "AssistantMessage":
        if getattr(msg, "error", None):
            _raise_sdk_error(f"SDK 消息错误：{msg.error}")
        return None  # 正文文本经 StreamEvent 到达；工具调用 V1 不消费
    if cls_name == "TaskNotificationMessage":
        if getattr(msg, "status", None) != "completed":
            return None
        return QoderEvent(
            "subagent",
            str(getattr(msg, "summary", "") or ""),
            {"task_id": str(getattr(msg, "task_id", "") or ""), "status": "completed"},
        )
    if cls_name == "ResultMessage":
        if getattr(msg, "is_error", False):
            errors = getattr(msg, "errors", None) or []
            text = "; ".join(errors) or str(getattr(msg, "subtype", "") or "unknown")
            _raise_sdk_error(text)
        return QoderEvent("result", str(getattr(msg, "result", "") or ""), {})
    return None


class FakeQoder:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def stream(
        self,
        token: str,
        prompt: str,
        *,
        cwd: Path | None,
        on_auth_expired: Callable[[], None] | None = None,
        subagents: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[QoderEvent]:
        for item in self._events:
            kind = item["kind"]
            if kind == "auth_expired":
                if on_auth_expired is not None:
                    on_auth_expired()
                raise TokenExpiredError("token expired（FakeQoder 注入）")
            if kind == "result":
                text = item["text"]
                if isinstance(text, dict):
                    text = json.dumps(text, ensure_ascii=False)
                yield QoderEvent("result", str(text), {})
            else:
                yield QoderEvent(kind, str(item.get("text", "")), dict(item.get("data", {})))
