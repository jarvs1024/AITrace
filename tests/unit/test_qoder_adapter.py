# tests/unit/test_qoder_adapter.py
import asyncio
import sys
import types

import pytest

from aitrace.ai.qoder import (
    CnSdkAdapter,
    FakeQoder,
    TokenExpiredError,
    _map_sdk_message,
    classify_sdk_exception,
)
from aitrace.core.settings import AISettings


async def _collect(stream) -> list:
    return [event async for event in stream]


def _script() -> list[dict]:
    return [
        {"kind": "delta", "text": "正在分析"},
        {"kind": "subagent", "text": "sub", "data": {"agent": "source_review"}},
        {"kind": "result", "text": '{"root_cause_category": "FW_DEFECT"}'},
    ]


def test_fake_qoder_streams_scripted_events() -> None:
    events = asyncio.run(_collect(FakeQoder(_script()).stream(token="t", prompt="p", cwd=None)))
    kinds = [e.kind for e in events]
    assert kinds == ["delta", "subagent", "result"]
    assert events[1].data == {"agent": "source_review"}
    assert "FW_DEFECT" in events[2].text


def test_fake_qoder_expiry_after_prior_events() -> None:
    script = [{"kind": "delta", "text": "x"}, {"kind": "auth_expired"}]

    async def run():
        collected = []
        try:
            async for event in FakeQoder(script).stream(token="t", prompt="p", cwd=None):
                collected.append(event.kind)
        except TokenExpiredError:
            return collected

    assert asyncio.run(run()) == ["delta"]


def test_classify_sdk_exception_known_codes() -> None:
    class _Expired(Exception):
        def __str__(self) -> str:
            return "token expired"

    assert classify_sdk_exception(_Expired())["code"] == "TOKEN_EXPIRED"
    assert classify_sdk_exception(RuntimeError("boom"))["code"] == "SDK_ERROR"
    assert (
        classify_sdk_exception(RuntimeError("rate limit exceeded"))["code"] == "TOKEN_RATE_LIMITED"
    )
    assert classify_sdk_exception(RuntimeError("HTTP 503 网关不可用"))["code"] == "HTTP_5XX"


def _real_sdk():
    try:
        import qodercn_agent_sdk.types as t
    except ImportError:  # pragma: no cover
        return None
    return t


def test_map_real_sdk_stream_text_delta() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "分析中"}},
    )
    ev = _map_sdk_message(msg)
    assert ev is not None and ev.kind == "delta" and ev.text == "分析中"


def test_map_real_sdk_thinking_delta_skipped() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}},
    )
    assert _map_sdk_message(msg) is None


def test_map_real_sdk_result_ok() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="s",
        result='{"root_cause_category": "FW_DEFECT"}',
    )
    ev = _map_sdk_message(msg)
    assert ev is not None and ev.kind == "result" and "FW_DEFECT" in ev.text


def test_map_real_sdk_result_error_with_expired_raises() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.ResultMessage(
        subtype="error_max_turns",
        duration_ms=0,
        duration_api_ms=0,
        is_error=True,
        num_turns=1,
        session_id="s",
        errors=["token expired during query"],
    )
    with pytest.raises(TokenExpiredError):
        _map_sdk_message(msg)


def test_map_real_sdk_assistant_auth_error_raises() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.AssistantMessage(content=[], model="m", error="authentication_failed")
    with pytest.raises(TokenExpiredError):
        _map_sdk_message(msg)


def test_map_real_sdk_subagent_notification() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    msg = t.TaskNotificationMessage(
        subtype="subagent_task_completed",
        data={},
        task_id="t1",
        status="completed",
        output_file="",
        summary="PROBE_OK 探查成功：链路正常",
        uuid="u",
        session_id="s",
    )
    ev = _map_sdk_message(msg)
    assert ev is not None and ev.kind == "subagent"
    assert "PROBE_OK" in ev.text
    assert ev.data == {"task_id": "t1", "status": "completed"}


def test_map_real_sdk_user_message_skipped() -> None:
    t = _real_sdk()
    if t is None:
        pytest.skip("qodercn_agent_sdk 未安装")
    assert _map_sdk_message(t.UserMessage(content="echo")) is None


def test_cn_adapter_builds_agents_dict_and_uses_sdk(monkeypatch) -> None:
    captured: dict = {}

    class _AgentDefinition:
        def __init__(self, **kwargs) -> None:
            captured["agent_kwargs"] = kwargs

    class _Options:
        pass

    class _Sdk:
        QoderAgentOptions = _Options
        AgentDefinition = _AgentDefinition

        @staticmethod
        def access_token(token: str) -> str:
            captured["auth"] = token
            return token

        @staticmethod
        async def query(*, prompt, options):
            captured["prompt"] = prompt
            captured["options"] = options

            class StreamEvent:
                def __init__(self) -> None:
                    self.event = {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "增量文本"},
                    }

            yield StreamEvent()

    fake = types.ModuleType("qodercn_agent_sdk")
    for name in ("QoderAgentOptions", "AgentDefinition", "access_token", "query"):
        setattr(fake, name, getattr(_Sdk, name))
    monkeypatch.setitem(sys.modules, "qodercn_agent_sdk", fake)

    async def run():
        adapter = CnSdkAdapter(AISettings())
        return await _collect(
            adapter.stream(
                "tk",
                "prompt-x",
                cwd=None,
                subagents=[
                    {"name": "source_agent", "description": "d", "prompt": "p"},
                ],
            )
        )

    events = asyncio.run(run())
    assert [e.text for e in events] == ["增量文本"]
    assert captured["prompt"] == "prompt-x"
    assert captured["auth"] == "tk"
    opts = captured["options"]
    assert opts.model == "GLM-5.3-Flash"
    assert opts.disallowed_tools == ["Bash", "Write", "Edit"]
    assert set(opts.agents) == {"source_agent"}
    assert captured["agent_kwargs"] == {"description": "d", "prompt": "p"}


def test_cn_adapter_rejects_subagent_without_name(monkeypatch) -> None:
    class _Options:
        pass

    class _Sdk:
        QoderAgentOptions = _Options

        @staticmethod
        def access_token(token: str) -> str:
            return token

        @staticmethod
        async def query(*, prompt, options):  # pragma: no cover
            raise AssertionError("query 不应被调用：子代理校验应先失败")

    fake = types.ModuleType("qodercn_agent_sdk")
    for name in ("QoderAgentOptions", "access_token", "query"):
        setattr(fake, name, getattr(_Sdk, name))
    monkeypatch.setitem(sys.modules, "qodercn_agent_sdk", fake)

    async def run():
        adapter = CnSdkAdapter(AISettings())
        async for _ in adapter.stream("tk", "p", cwd=None, subagents=[{"prompt": "p"}]):
            pass

    with pytest.raises(RuntimeError, match="name"):
        asyncio.run(run())


def _capture_sdk_options(monkeypatch, settings: AISettings) -> dict:
    captured: dict = {}

    class _Options:
        pass

    class _Sdk:
        QoderAgentOptions = _Options

        @staticmethod
        def access_token(token: str) -> str:
            captured["auth"] = token
            return token

        @staticmethod
        async def query(*, prompt, options):
            captured["options"] = options
            if False:  # pragma: no cover
                yield None

    fake = types.ModuleType("qodercn_agent_sdk")
    for name in ("QoderAgentOptions", "access_token", "query"):
        setattr(fake, name, getattr(_Sdk, name))
    monkeypatch.setitem(sys.modules, "qodercn_agent_sdk", fake)

    async def run():
        adapter = CnSdkAdapter(settings)
        async for _ in adapter.stream("tk", "p", cwd=None):
            pass

    asyncio.run(run())
    return captured


def test_cn_adapter_passes_vpc_endpoint_and_control_timeout(monkeypatch) -> None:
    captured = _capture_sdk_options(
        monkeypatch,
        AISettings(vpc_endpoint="twsc.vpc.qoder.com.cn", sdk_control_timeout_ms=150_000),
    )
    opts = captured["options"]
    assert opts.vpc_endpoint == "twsc.vpc.qoder.com.cn"
    assert opts.control_request_timeout_ms == 150_000


def test_cn_adapter_default_vpc_endpoint_none_and_timeout_default(monkeypatch) -> None:
    captured = _capture_sdk_options(monkeypatch, AISettings())
    opts = captured["options"]
    assert opts.vpc_endpoint is None
    assert opts.control_request_timeout_ms == 180_000
