# src/aitrace/cleaning/fingerprint.py
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from aitrace.cleaning.types import KeyLine


def _digest(*parts: object) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def exact_fingerprint(
    script_path: str, line_no: int, exc_type: str | None, err_code: str | None, device_id: str
) -> str:
    return _digest(script_path, line_no, exc_type, err_code, device_id)


def stable_fingerprint(
    exc_type: str | None, err_code: str | None, hardware_type: str, category: str
) -> str:
    return _digest(exc_type, err_code, hardware_type, category)


@dataclass(frozen=True)
class DedupGroup:
    exact_fp: str
    count: int
    first_line: int
    last_line: int
    sample: str


def dedup_key_lines(
    key_lines: tuple[KeyLine, ...],
    exact_fn: Callable[[KeyLine], str],
) -> tuple[DedupGroup, ...]:
    ordered: list[tuple[str, KeyLine]] = []
    for line in key_lines:
        ordered.append((exact_fn(line), line))
    merged: dict[str, list[KeyLine]] = {}
    for fp, line in ordered:
        merged.setdefault(fp, []).append(line)
    groups = []
    for fp, items in merged.items():
        groups.append(
            DedupGroup(
                exact_fp=fp,
                count=len(items),
                first_line=items[0].line_no,
                last_line=items[-1].line_no,
                sample=items[0].text,
            )
        )
    return tuple(groups)
