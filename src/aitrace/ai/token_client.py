# src/aitrace/ai/token_client.py
from __future__ import annotations

from datetime import UTC, datetime

import httpx

from aitrace.observability.metrics import MetricsRegistry

_RETRYABLE_CODES = {"TOKEN_INVALID", "TOKEN_EXPIRED", "TOKEN_RATE_LIMITED"}


class TokenFetchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status


class TokenGrant:
    def __init__(self, token: str, expires_at: datetime) -> None:
        self.token = token
        self.expires_at = expires_at


class TokenFetchClient:
    def __init__(
        self,
        *,
        base_url: str,
        read_token: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
        registry: MetricsRegistry | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._read_token = read_token
        self._timeout = timeout_seconds
        self._transport = transport
        self._registry = registry

    def _record_fetch(self, ok: bool) -> None:
        if self._registry is None:
            return
        key = "token_fetch_ok" if ok else "token_fetch_fail"
        self._registry.counter(f"{key}_total").inc()
        self._registry.window_counter(key).inc()

    def fetch(
        self,
        *,
        product_id: str,
        batch_id: str,
        case_id: str,
        trigger_user_id: str,
        action: str,
    ) -> TokenGrant:
        params = {
            "product_id": product_id,
            "batch_id": batch_id,
            "case_id": case_id,
            "trigger_user_id": trigger_user_id,
            "action": action,
        }
        with httpx.Client(
            transport=self._transport, timeout=self._timeout, trust_env=False
        ) as client:
            try:
                response = client.get(
                    f"{self._base_url}/api/v1/aitest/token",
                    params=params,
                    headers={"X-Aitest-Read-Token": self._read_token},
                )
            except httpx.HTTPError as exc:
                self._record_fetch(ok=False)
                raise TokenFetchError(
                    "TIMEOUT", f"令牌接口不可达：{type(exc).__name__}", retryable=True
                ) from exc
        body = _safe_json(response)
        if response.status_code == 200:
            token = body.get("token")
            expires_at = body.get("expires_at")
            if not isinstance(token, str) or not token:
                self._record_fetch(ok=False)
                raise TokenFetchError("BAD_GRANT", "令牌接口返回缺少 token 字段", retryable=False)
            parsed = _parse_expiry(expires_at)
            if parsed is None:
                self._record_fetch(ok=False)
                raise TokenFetchError(
                    "BAD_GRANT", "令牌接口返回的 expires_at 非法", retryable=False
                )
            self._record_fetch(ok=True)
            return TokenGrant(token=token, expires_at=parsed)
        self._record_fetch(ok=False)
        code = str(body.get("code") or "UNKNOWN")
        message = str(body.get("message") or "令牌接口返回错误")
        retryable = code in _RETRYABLE_CODES or response.status_code >= 500
        raise TokenFetchError(
            code,
            f"令牌获取失败：{message}",
            retryable=retryable,
            http_status=response.status_code,
        )


def _safe_json(response: httpx.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_expiry(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
