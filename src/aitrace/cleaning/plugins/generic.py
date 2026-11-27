from __future__ import annotations

import re
from datetime import datetime

from aitrace.cleaning.types import ParsedSource, SourceKind

_MAX_CHARS = 200_000
_BINARY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class GenericPlugin:
    key = "generic"

    def parse(self, text: str, *, boot_utc: datetime | None = None) -> ParsedSource:
        cleaned = _BINARY.sub("", text)
        truncated = len(cleaned) > _MAX_CHARS
        if truncated:
            cleaned = cleaned[:_MAX_CHARS]
        return ParsedSource(
            SourceKind.GENERIC,
            tuple(cleaned.splitlines()),
            metrics={"truncated": truncated, "events_parsed": 0},
        )
