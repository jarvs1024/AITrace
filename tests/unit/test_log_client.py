from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aitrace.ingest.log_client import LogClient, LogFetchError


def _client(handler) -> LogClient:
    return LogClient(
        base_url="http://aitest.test",
        read_token="mock-read-token",
        transport=httpx.MockTransport(handler),
    )


def test_download_to_writes_full_body_and_passes_params(tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["url"] = str(request.url)
        calls["token"] = request.headers.get("X-Aitest-Read-Token")
        calls["range"] = request.headers.get("range")
        return httpx.Response(200, content=b"[1763099900] line one\n[1763100100] line two\n")

    client = _client(handler)
    dest = tmp_path / "deep" / "test.log"
    size = client.download_to(
        path="var/log/aitest/CASE-1021/test.log",
        dest=dest,
        failed_at_utc=1763100000,
    )
    assert size == len(dest.read_bytes())
    assert dest.read_text(encoding="utf-8").startswith("[1763099900] line one")
    assert calls["url"] == (
        "http://aitest.test/api/v1/aitest/logs?"
        "path=var%2Flog%2Faitest%2FCASE-1021%2Ftest.log&failed_at_utc=1763100000"
    )
    assert calls["token"] == "mock-read-token"
    assert calls["range"] is None


def test_resume_sends_range_and_appends(tmp_path: Path) -> None:
    dest = tmp_path / "test.log"
    dest.write_bytes(b"abcd")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range", "")
        return httpx.Response(206, content=b"efghijk", headers={"Content-Range": "bytes 4-10/11"})

    size = _client(handler).download_to(path="var/log/aitest/CASE-1021/test.log", dest=dest)
    assert seen["range"] == "bytes=4-"
    assert size == 11
    assert dest.read_bytes() == b"abcdefghijk"


def test_download_404_is_not_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "LOG_NOT_FOUND", "message": "日志不存在"})

    with pytest.raises(LogFetchError) as exc_info:
        _client(handler).download_to(path="var/log/nope.log", dest=tmp_path / "nope.log")
    error = exc_info.value
    assert error.code == "LOG_NOT_FOUND"
    assert error.retryable is False


def test_traversal_rejected_locally_without_request() -> None:
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, content=b"x")

    with pytest.raises(LogFetchError) as exc_info:
        _client(handler).download_to(path="../../etc/passwd", dest=Path("/tmp/x.log"))
    assert exc_info.value.code == "PATH_FORBIDDEN"
    assert exc_info.value.retryable is False
    assert called == []


def test_network_error_is_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LogFetchError) as exc_info:
        _client(handler).download_to(path="var/log/a.log", dest=tmp_path / "a.log")
    assert exc_info.value.retryable is True


def test_download_too_large_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100, headers={"Content-Length": "100"})

    with pytest.raises(LogFetchError) as exc_info:
        _client(handler).download_to(path="var/log/big.log", dest=tmp_path / "big.log", max_bytes=8)
    assert exc_info.value.code == "LOG_TOO_LARGE"


def test_list_logs_parses_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/aitest/logs"
        assert request.url.params["batch_id"] == "B1"
        assert request.url.params["case_id"] == "C1"
        return httpx.Response(
            200,
            json={
                "batch_id": "B1",
                "items": [
                    {"path": "var/log/aitest/C1/test.log", "size_bytes": 123},
                    {"path": "var/log/aitest/C1/uart.log", "size_bytes": 45},
                ],
            },
        )

    items = _client(handler).list_logs(batch_id="B1", case_id="C1")
    assert [item["path"] for item in items] == [
        "var/log/aitest/C1/test.log",
        "var/log/aitest/C1/uart.log",
    ]
