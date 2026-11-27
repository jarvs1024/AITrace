from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    TEST = "test"
    UART = "uart"
    DMESG = "dmesg"
    SCHEDULER = "scheduler"
    GENERIC = "generic"
    IMAGE = "image"


@dataclass(frozen=True)
class Event:
    line_no: int
    ts_utc: datetime | None
    rel_ms: float | None
    tick: int | None
    kind: str
    text: str


@dataclass(frozen=True)
class KeyLine:
    line_no: int
    kind: str
    text: str
    ts_utc: datetime | None = None
    rel_ms: float | None = None
    tick: int | None = None


@dataclass(frozen=True)
class ParsedSource:
    source: SourceKind
    raw_lines: tuple[str, ...]
    events: tuple[Event, ...] = ()
    key_lines: tuple[KeyLine, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
