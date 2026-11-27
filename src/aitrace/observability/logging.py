"""结构化 JSON 日志（spec §7.4 三本账载体）与敏感字段掩码。

统一字段: ts / level / component / event / msg 上下文任意字段；
trace 字段由调用方随 log_event 附带（trace_id = {product_id}:{batch_id}）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_SECRET_RE = re.compile(
    r"(^|[_.-])(token|secret|password|passwd|credential|authorization|api[_-]?key)($|[_.-])",
    re.IGNORECASE,
)
_REDACTED = "<已脱敏>"


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _REDACTED if _is_secret_key(k) else _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _is_secret_key(key: str) -> bool:
    return _SECRET_RE.search(key) is not None


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        try:
            payload = json.loads(message)
        except ValueError:
            payload = {"msg": str(message)}
        if not isinstance(payload, dict):
            payload = {"msg": str(payload)}
        masked = _mask(payload)
        return json.dumps(masked, ensure_ascii=False, default=str, sort_keys=True)


class _JsonHandler(logging.StreamHandler):  # 覆写 formatter 类型以便测试/运行期识别
    pass


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not any(isinstance(h, _JsonHandler) for h in root.handlers):
        handler = _JsonHandler()
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)


def log_event(component: str, event: str, *, level: str = "INFO", **fields: Any) -> None:
    payload: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "level": level,
        "component": component,
        "event": event,
        **fields,
    }
    payload = _mask(payload)
    logger = logging.getLogger(f"aitrace.{component}")
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        json.dumps(payload, ensure_ascii=False, default=str),
    )
