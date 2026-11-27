from __future__ import annotations

import re
from datetime import datetime

from aitrace.cleaning.plugins import _TICK
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(r"\b(ASSERT|PANIC|FATAL|ABORT|TIMEOUT|WATCHDOG|RESET|ERR)\b")
_HEARTBEAT = re.compile(r"(heartbeat|^tick\s+\d+)", re.IGNORECASE)
_BUILD = re.compile(r"Build\s+ID[:\s]*(\S+)")
_CMD = re.compile(r"\bcmd\b", re.IGNORECASE)
_PRINTABLE = re.compile(r"[^\x20-\x7e\u4e00-\u9fff\n]")  # 保留 ASCII 可打印与中文


class UartPlugin:
    key = "uart"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        cleaned = _PRINTABLE.sub("", text)
        lines = cleaned.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        folded = 0
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            m_tick = _TICK.search(line)
            tick = int(m_tick.group(1)) if m_tick else None
            if _HEARTBEAT.search(line) and events and events[-1].kind == "heartbeat":
                folded += 1
                continue
            kind = (
                "heartbeat"
                if _HEARTBEAT.search(line)
                else (
                    "hard_error"
                    if _HARD.search(line)
                    else "command"
                    if _CMD.search(line)
                    else "normal"
                )
            )
            events.append(Event(line_no, None, None, tick, kind, line))
            if kind == "hard_error" or _BUILD.search(line) or tick is not None:
                key_lines.append(KeyLine(line_no, kind, line, tick=tick))
        return ParsedSource(
            SourceKind.UART,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"heartbeat_folded": folded, "events_parsed": len(events)},
        )
