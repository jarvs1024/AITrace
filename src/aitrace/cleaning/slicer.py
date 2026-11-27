from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aitrace.cleaning.align import AlignedContext, Anchor
from aitrace.cleaning.types import ParsedSource, SourceKind
from aitrace.profiles.models import SliceParams


@dataclass(frozen=True)
class Slice:
    source: str
    window: str  # baseline / error_window / tail
    lines: tuple[str, ...]
    size_bytes: int


@dataclass
class Slices:
    baseline: Slice | None = None
    error_window: Slice | None = None
    tail: Slice | None = None
    full: bool = False
    shrinks: list[str] = field(default_factory=list)
    exemptions: int = 0


def _source_bytes(ps: ParsedSource) -> int:
    return sum(len(line.encode("utf-8")) + 1 for line in ps.raw_lines)


def _keyline_times(ps: ParsedSource, ctx: AlignedContext) -> dict[int, datetime]:
    times: dict[int, datetime] = {}
    if ps.source == SourceKind.UART:
        offset = ctx.offset_ms.get("uart")
        last_anchor_ms: int | None = None
        for key in ps.key_lines:
            if key.tick is not None and offset is not None:
                last_anchor_ms = offset + key.tick  # tick 单位 1ms
            if last_anchor_ms is not None:
                # 无 tick 的关键行沿用最近一个 tick 锚点的时间（与 align.py 的顺延规则一致）
                times[key.line_no] = datetime.fromtimestamp(last_anchor_ms / 1000, tz=UTC)
    else:
        for key in ps.key_lines:
            if key.ts_utc is not None:
                times[key.line_no] = key.ts_utc
    return times


def _mk(source: str, window: str, lines: list[str]) -> Slice:
    frozen = tuple(lines)
    return Slice(source, window, frozen, sum(len(line.encode("utf-8")) + 1 for line in frozen))


def build_slices(
    parsed: dict[str, ParsedSource],
    ctx: AlignedContext,
    anchor: Anchor,
    params: SliceParams,
) -> Slices:
    total = sum(_source_bytes(ps) for ps in parsed.values())
    if total <= params.small_log_full_threshold_bytes:
        return Slices(full=True)
    before = anchor.ts_utc - timedelta(seconds=params.first_error_before_s)
    after = anchor.ts_utc + timedelta(seconds=params.first_error_after_s)
    baseline_lines: list[str] = []
    tail_lines: list[str] = []
    window_lines: list[str] = []
    exemptions = 0
    for source, ps in parsed.items():
        lines = ps.raw_lines
        base_n = min(params.baseline_lines, len(lines))
        tail_n = min(params.tail_lines, len(lines))
        times = _keyline_times(ps, ctx)
        in_window: set[int] = set()
        for line_no, ts in times.items():
            if before <= ts <= after:
                in_window.add(line_no)
        for key in ps.key_lines:
            if key.line_no not in in_window:
                in_window.add(key.line_no)  # 必保行豁免
                exemptions += 1
        for idx, line in enumerate(lines, start=1):
            tagged = f"{source}:{line}"
            if idx <= base_n:
                baseline_lines.append(tagged)
            if idx > len(lines) - tail_n:
                tail_lines.append(tagged)
            if idx in in_window:
                window_lines.append(tagged)
    window_bytes = sum(len(line.encode("utf-8")) + 1 for line in window_lines)
    shrinks: list[str] = []
    if window_bytes > params.first_error_window_budget_bytes:
        shrinks.append("baseline")
        baseline_lines = baseline_lines[: len(baseline_lines) // 2]
        shrinks.append("tail")
        tail_lines = tail_lines[: len(tail_lines) // 2]
        shrinks.append("error_window")
        window_lines = window_lines[-2000:]  # 保留事件边界对齐后的尾部核心
    return Slices(
        baseline=_mk("combined", "baseline", baseline_lines),
        error_window=_mk("combined", "error_window", window_lines),
        tail=_mk("combined", "tail", tail_lines),
        full=False,
        shrinks=shrinks,
        exemptions=exemptions,
    )
