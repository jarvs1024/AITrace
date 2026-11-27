from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.errors import AitraceError
from aitrace.persistence.models import AuditLog

GENESIS_HASH = "0" * 64
_CHAIN_LOCK_KEY = 0x41495452  # "AITR"
_VERIFY_CACHE_SECONDS = 60.0

_JSONB_SCALAR_TYPES = (str, int, bool, type(None))
_FLOAT_REJECTED_MSG = "审计明细不允许浮点数（JSONB 会规范化 1.0→1 破坏哈希链），请使用字符串或整数"


def _validate_jsonb_detail(value: Any, path: str = "detail") -> None:
    value_type = type(value)
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"审计明细键必须为字符串：{path}")
            _validate_jsonb_detail(item, f"{path}.{key}")
        return
    if value_type is list:
        for index, item in enumerate(value):
            _validate_jsonb_detail(item, f"{path}[{index}]")
        return
    if value_type in _JSONB_SCALAR_TYPES:
        return
    if value_type is float:
        raise ValueError(_FLOAT_REJECTED_MSG)
    raise ValueError(
        f"审计明细包含不受支持的类型 {value_type.__name__}（{path}），"
        "仅允许 str/int/bool/None/list/dict"
    )


def _canonical(ts: datetime, actor: str, action: str, target: str, detail: dict[str, Any]) -> str:
    return json.dumps(
        {
            "ts": ts.astimezone(UTC).isoformat(timespec="microseconds"),
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


class AuditService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        blocked: Callable[[], bool] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._blocked = blocked
        self._last_verify_at: float | None = None
        self._last_verify_ok: bool | None = None

    def append(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any],
        now: datetime | None = None,
        blocked: Callable[[], bool] | None = None,
    ) -> str:
        guard = blocked if blocked is not None else self._blocked
        if guard is not None and guard():
            raise AitraceError(
                "AUDIT_CHAIN_BROKEN",
                "审计哈希链校验失败，审计写入已熔断；请排查审计表完整性，修复后重启服务恢复",
                http_status=503,
            )
        _validate_jsonb_detail(detail)
        ts = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})
            prev_hash = (
                session.execute(
                    select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
                ).scalar_one_or_none()
                or GENESIS_HASH
            )
            digest = _digest(prev_hash, _canonical(ts, actor, action, target, detail))
            session.add(
                AuditLog(
                    ts=ts,
                    actor=actor,
                    action=action,
                    target=target,
                    detail=detail,
                    prev_hash=prev_hash,
                    hash=digest,
                )
            )
            return digest

    def verify_chain(self) -> bool:
        now_mono = time.monotonic()
        if (
            self._last_verify_at is not None
            and self._last_verify_ok is not None
            and now_mono - self._last_verify_at < _VERIFY_CACHE_SECONDS
        ):
            return self._last_verify_ok
        ok = self._verify_chain_now()
        self._last_verify_at = now_mono
        self._last_verify_ok = ok
        return ok

    def _verify_chain_now(self) -> bool:
        with self._session_factory() as session:
            prev_hash = GENESIS_HASH
            for row in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
                if row.prev_hash != prev_hash:
                    return False
                expected = _digest(
                    prev_hash, _canonical(row.ts, row.actor, row.action, row.target, row.detail)
                )
                if row.hash != expected:
                    return False
                prev_hash = row.hash
            return True
