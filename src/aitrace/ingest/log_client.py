# src/aitrace/ingest/log_client.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class LogFetchError(RuntimeError):
    """拉取 aitest 日志失败；code 与 retryable 供错误信息与重试策略使用，不含令牌。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


_DEFAULT_MAX_BYTES = 512 * 1024 * 1024
_CHUNK = 256 * 1024


class LogClient:
    """aitest 日志清单/流式下载客户端（spec §6.3）：Range 续传、失败时刻截断、路径白名单。"""

    def __init__(
        self,
        *,
        base_url: str,
        read_token: str,
        timeout_seconds: float = 30.0,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._read_token = read_token
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=self._timeout, trust_env=False)

    @staticmethod
    def _check_path(path: str) -> None:
        if path.startswith("/") or ".." in Path(path).parts:
            raise LogFetchError("PATH_FORBIDDEN", f"日志路径越界：{path}", retryable=False)

    def list_logs(self, *, batch_id: str, case_id: str) -> list[dict[str, Any]]:
        with self._client() as client:
            try:
                response = client.get(
                    f"{self._base_url}/api/v1/aitest/logs",
                    params={"batch_id": batch_id, "case_id": case_id},
                    headers={"X-Aitest-Read-Token": self._read_token},
                )
            except httpx.HTTPError as exc:
                raise LogFetchError(
                    "LOG_FETCH_NETWORK", f"日志清单接口不可达：{type(exc).__name__}", retryable=True
                ) from exc
        if response.status_code != 200:
            raise LogFetchError(
                _error_code(response),
                _error_message(response),
                retryable=response.status_code >= 500,
            )
        body = response.json()
        items = body.get("items")
        if not isinstance(items, list):
            raise LogFetchError("LOG_BAD_RESPONSE", "日志清单响应缺少 items 数组", retryable=False)
        return [item for item in items if isinstance(item, dict)]

    def download_to(
        self,
        *,
        path: str,
        dest: Path,
        failed_at_utc: int | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """流式下载日志到 dest；dest 已存在时按 Range 续传；返回最终文件字节数。"""
        self._check_path(path)
        limit = max_bytes if max_bytes is not None else self._max_bytes
        offset = dest.stat().st_size if dest.is_file() else 0
        params: dict[str, Any] = {"path": path}
        if failed_at_utc is not None:
            params["failed_at_utc"] = failed_at_utc
        headers = {"X-Aitest-Read-Token": self._read_token}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        with self._client() as client:
            try:
                response = client.get(
                    f"{self._base_url}/api/v1/aitest/logs", params=params, headers=headers
                )
                stream = _StreamGuard(client=client, response=response, limit=limit, offset=offset)
                with stream:
                    return stream.drain(dest)
            except httpx.HTTPError as exc:
                raise LogFetchError(
                    "LOG_FETCH_NETWORK", f"日志下载接口不可达：{type(exc).__name__}", retryable=True
                ) from exc


class _StreamGuard:
    """把响应体按块写入目标文件；做大小上限与状态码校验，写坏的文件不残留。"""

    def __init__(
        self, *, client: httpx.Client, response: httpx.Response, limit: int, offset: int
    ) -> None:
        self._client = client
        self._response = response
        self._limit = limit
        self._offset = offset
        self._written = 0

    def __enter__(self) -> _StreamGuard:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def drain(self, dest: Path) -> int:
        response = self._response
        if response.status_code not in (200, 206):
            raise LogFetchError(
                _error_code(response),
                _error_message(response),
                retryable=response.status_code >= 500,
            )
        declared = int(response.headers.get("Content-Length", 0))
        start = self._offset if response.status_code == 206 else 0
        total = start + (declared if declared else 0)
        if total > self._limit:
            raise LogFetchError(
                "LOG_TOO_LARGE", f"日志大小 {total} 超过上限 {self._limit}", retryable=False
            )
        mode = "ab" if response.status_code == 206 else "wb"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with dest.open(mode) as out:
                for chunk in response.iter_bytes(chunk_size=_CHUNK):
                    self._written += len(chunk)
                    if self._written + start > self._limit:
                        raise LogFetchError(
                            "LOG_TOO_LARGE",
                            f"日志大小超过上限 {self._limit}",
                            retryable=False,
                        )
                    out.write(chunk)
        except LogFetchError:
            raise
        return start + self._written


def _error_code(response: httpx.Response) -> str:
    try:
        code = response.json().get("code")
    except ValueError:
        code = None
    if isinstance(code, str) and code:
        return code
    return "LOG_FETCH_HTTP_ERROR"


def _error_message(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
    except ValueError:
        message = None
    if isinstance(message, str) and message:
        return message
    return f"日志接口返回 {response.status_code}"
