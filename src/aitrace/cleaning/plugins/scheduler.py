from __future__ import annotations

import re
from datetime import datetime

from aitrace.cleaning.plugins import _ts
from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_HARD = re.compile(
    r"(锁板冲突|lock\s*conflict|Agent\s*失联|agent\s*lost|任务取消|task\s*cancel)",
    re.IGNORECASE,
)
_STATE = re.compile(r"\bSTATE\s+(\S+)")


class SchedulerPlugin:
    key = "scheduler"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        lines = text.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        compressed = 0
        for line_no, line in enumerate(lines, start=1):
            ts = _ts("", line)
            m = _STATE.search(line)
            if m and events and events[-1].kind == "state" and events[-1].text.endswith(m.group(1)):
                compressed += 1
                continue
            if _HARD.search(line):
                kind = "hard_error"
            elif m:
                kind = "state"
            else:
                kind = "normal"
            events.append(Event(line_no, ts, None, None, kind, line))
            if kind == "hard_error" or (
                kind == "state" and m and m.group(1) in {"LOCK_DEVICE", "EXIT", "ENV_INIT"}
            ):
                key_lines.append(KeyLine(line_no, kind, line, ts_utc=ts))
        return ParsedSource(
            SourceKind.SCHEDULER,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"states_compressed": compressed, "events_parsed": len(events)},
        )
