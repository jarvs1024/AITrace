from __future__ import annotations

import secrets
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidIdGenerator:
    def new(self, prefix: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        return f"{prefix}-{stamp}-{secrets.token_hex(4).upper()}"
