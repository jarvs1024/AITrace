from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Protocol

from aitrace.cleaning.types import ParsedSource

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Z 纳入捕获组：replace("Z", "+00:00") 后 fromisoformat 才能得到带时区的 UTC 时间
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z?)\s")
_TICK = re.compile(r"@TICK\s+(\d+)")
_CMD_TICK = re.compile(r"@CMD\s+uart_tick=(\d+)")


class CleaningPlugin(Protocol):
    key: str

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource: ...


def _ts(prefix: str, line: str) -> datetime | None:
    m = _TS.match(line)
    if not m:
        return None
    raw = m.group(1)
    try:
        # 统一按 UTC 解释（无 Z 的时间戳同样视为 UTC），不依赖本机时区，保证确定性
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=UTC)
    except ValueError:
        return None
