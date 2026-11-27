from __future__ import annotations

import re
from datetime import datetime

from aitrace.cleaning.plugins import _ANSI, _CMD_TICK, _ts
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(r"(Traceback|AssertionError|\bERROR\b|\bFAILED\b|Fatal)")
_CMD = re.compile(r"\bCMD\b")
_EXIT = re.compile(r"exit code\s+(-?\d+)")


class TestlogPlugin:
    key = "test"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = _ANSI.sub("", raw).rstrip("\r")
            ts = _ts("", line)
            m_cmd = _CMD_TICK.search(line)
            tick = int(m_cmd.group(1)) if m_cmd else None
            if _HARD.search(line):
                kind = "hard_error"
            elif _CMD.search(line):
                kind = "command"
            else:
                kind = "normal"
            events.append(Event(line_no, ts, None, tick, kind, line))
            if kind == "hard_error" or _EXIT.search(line) or tick is not None:
                key_lines.append(KeyLine(line_no, kind, line, ts_utc=ts, tick=tick))
        return ParsedSource(
            SourceKind.TEST,
            tuple(_ANSI.sub("", raw).rstrip("\r") for raw in text.splitlines()),
            tuple(events),
            tuple(key_lines),
            {"events_parsed": len(events)},
        )
