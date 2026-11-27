from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from aitrace.cleaning.types import Event, ParsedSource, SourceKind

_EXC = re.compile(
    r"(?:Traceback|raise)\s*\(?([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|AssertionError))|"
    r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|AssertionError))\s*:"
)
_ERR_CODE = re.compile(
    r"(?:err(?:or)?[=:]?\s*|err_code=|exit code\s+)(-?(?:0x[0-9a-fA-F]+|\d+))",
    re.IGNORECASE,
)
_EXIT_CODE = re.compile(r"exit code\s+(-?\d+)", re.IGNORECASE)

_KEYWORDS = (
    ("power", ("power", "suspend", "resume", "掉电", "掉盘")),
    ("timeout", ("timeout", "超时", "waiting", "expire")),
    ("pcie", ("aer", "pcie", "link down", "linkdown")),
    ("protocol", ("protocol", "phase", "rsp", "cmd")),
    ("assert", ("assert", "panic", "fatal", "bug")),
    ("io", ("i/o", "io error", "eio", "坏块", "read error", "write error")),
    ("memory", ("out of memory", "oom", "内存")),
)


@dataclass(frozen=True)
class FirstError:
    ts_utc: datetime
    exc_type: str | None
    err_code: str | None
    line_no: int
    excerpt: str


def locate_first_error(events: tuple[Event, ...]) -> FirstError | None:
    hard = [event for event in events if event.kind == "hard_error"]
    if not hard:
        return None
    earliest_ts = next((event.ts_utc for event in hard if event.ts_utc is not None), None)
    for event in hard:
        exc = _EXC.search(event.text)
        code = _ERR_CODE.search(event.text)
        if code is None:
            code = _EXIT_CODE.search(event.text)
        if exc is None and code is None:
            continue
        return FirstError(
            ts_utc=earliest_ts,  # type: ignore[arg-type]  # 测试日志必有时钟
            exc_type=exc.group(1) or exc.group(2) if exc else None,
            err_code=code.group(1) if code else None,
            line_no=event.line_no,
            excerpt=event.text,
        )
    return None


def keyword_category(excerpt: str) -> str:
    lowered = excerpt.lower()
    for category, needles in _KEYWORDS:
        if any(needle in lowered for needle in needles):
            return category
    return "other"


def relevant_sources(parsed: dict[str, ParsedSource]) -> set[str]:
    relevant = {"test"}
    for name, kind in (("uart", SourceKind.UART), ("dmesg", SourceKind.DMESG)):
        source = parsed.get(name, ParsedSource(kind, ()))
        if any(key.kind == "hard_error" for key in source.key_lines):
            relevant.add(name)
    return relevant
