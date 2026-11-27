# tests/unit/test_token_client.py
from datetime import UTC, datetime

import httpx
import pytest

from aitrace.ai.token_client import TokenFetchClient, TokenFetchError


def _client(response: httpx.Response) -> TokenFetchClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    transport = httpx.MockTransport(handler)
    client = TokenFetchClient(
        base_url="http://mock-aitest",
        read_token="secret",
        timeout_seconds=5.0,
        transport=transport,
    )
    return client


def _grant_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"token": "tok-123", "expires_at": "2026-11-26T12:00:00Z"},
    )


def test_fetch_returns_grant_and_sends_binding_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("x-aitest-read-token")
        return _grant_response()

    client = TokenFetchClient(
        base_url="http://mock-aitest",
        read_token="secret",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    grant = client.fetch(
        product_id="ssd_9560",
        batch_id="B1",
        case_id="C1",
        trigger_user_id="zhangsan",
        action="auto",
    )
    assert grant.token == "tok-123"
    assert grant.expires_at == datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert "/api/v1/aitest/token" in seen["url"]
    assert "product_id=ssd_9560" in seen["url"]
    assert "trigger_user_id=zhangsan" in seen["url"]
    assert "action=auto" in seen["url"]
    assert seen["auth"] == "secret"


def test_http_error_maps_to_non_retryable_codes() -> None:
    for status, code in ((404, "TOKEN_NOT_FOUND"), (403, "TOKEN_FORBIDDEN")):
        with pytest.raises(TokenFetchError) as exc_info:
            _client(httpx.Response(status, json={"code": code, "message": "x"})).fetch(
                product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto"
            )
        assert exc_info.value.code == code
        assert exc_info.value.retryable is False


def test_recoverable_codes() -> None:
    for status, code in (
        (401, "TOKEN_INVALID"),
        (410, "TOKEN_EXPIRED"),
        (429, "TOKEN_RATE_LIMITED"),
    ):
        with pytest.raises(TokenFetchError) as exc_info:
            _client(httpx.Response(status, json={"code": code, "message": "x"})).fetch(
                product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto"
            )
        assert exc_info.value.code == code
        assert exc_info.value.retryable is True


def test_5xx_and_timeout_recoverable() -> None:
    with pytest.raises(TokenFetchError) as exc_info:
        _client(httpx.Response(502, json={"code": "UPSTREAM", "message": "x"})).fetch(
            product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto"
        )
    assert exc_info.value.retryable is True

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    client = TokenFetchClient(
        base_url="http://mock-aitest",
        read_token="secret",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(TokenFetchError) as exc_info:
        client.fetch(product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto")
    assert exc_info.value.code == "TIMEOUT"
    assert exc_info.value.retryable is True


def test_400_unknown_code_is_non_retryable() -> None:
    with pytest.raises(TokenFetchError) as exc_info:
        _client(httpx.Response(400, json={"code": "UNKNOWN", "message": "x"})).fetch(
            product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto"
        )
    assert exc_info.value.retryable is False


def test_error_message_never_contains_token() -> None:
    response = httpx.Response(200, json={"token": "SUPER-SECRET-TOKEN", "expires_at": "x"})
    client = _client(response)
    with pytest.raises(TokenFetchError) as exc_info:
        client.fetch(product_id="p", batch_id="b", case_id="c", trigger_user_id="u", action="auto")
    assert "SUPER-SECRET-TOKEN" not in str(exc_info.value)
