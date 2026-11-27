from __future__ import annotations

import re
from datetime import datetime, timedelta

from aitrace.cleaning.types import Event, KeyLine, ParsedSource, SourceKind

_REL = re.compile(r"^\[\s*(\d+)\.(\d{1,6})\]\s(.*)$")
# Booting 计入白名单：引导行属于 dmesg 事件流中的合法上下文
_WHITELIST = re.compile(
    r"(nvme|pcie|pci|cxl|ahci|scsi|block|ext4|xfs|memory|cpu|booting|BUG:|Call Trace)",
    re.IGNORECASE,
)
_HARD = re.compile(
    r"(AER|Call Trace|Link Down|Controller Reset|protocol error|BUG:)",
    re.IGNORECASE,
)


class DmesgPlugin:
    key = "dmesg"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        lines = text.splitlines()
        events: list[Event] = []
        key_lines: list[KeyLine] = []
        folded = 0
        last_body: str | None = None
        for line_no, line in enumerate(lines, start=1):
            m = _REL.match(line)
            if not m:
                continue
            sec, frac, body = int(m.group(1)), m.group(2) or "", m.group(3) or ""
            rel_ms = sec * 1000.0 + int(frac.ljust(6, "0")) / 1000.0
            ts = (boot_utc + timedelta(milliseconds=rel_ms)) if boot_utc else None
            # 重复折叠先于白名单过滤：非白名单行（如 usb）同样折叠但不进事件流
            if "error" in body.lower() and body == last_body:
                folded += 1
                continue
            last_body = body
            if not _WHITELIST.search(body):
                continue
            kind = (
                "hard_error"
                if _HARD.search(body)
                else "state"
                if "probe" in body.lower() or "attach" in body.lower()
                else "normal"
            )
            events.append(Event(line_no, ts, rel_ms, None, kind, body))
            if kind == "hard_error":
                key_lines.append(KeyLine(line_no, kind, body, ts_utc=ts, rel_ms=rel_ms))
        return ParsedSource(
            SourceKind.DMESG,
            tuple(lines),
            tuple(events),
            tuple(key_lines),
            {"repeats_folded": folded, "events_parsed": len(events)},
        )
