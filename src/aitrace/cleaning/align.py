from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from aitrace.cleaning.first_error import FirstError
from aitrace.cleaning.types import ParsedSource


@dataclass(frozen=True)
class Anchor:
    ts_utc: datetime
    source: str
    cascaded: bool


@dataclass
class AlignedContext:
    aligned: dict[str, bool] = field(default_factory=dict)
    offset_ms: dict[str, int] = field(default_factory=dict)
    times: dict[str, dict[str, datetime]] = field(default_factory=dict)
    alignment_rate: float = 0.0
    failed_at_utc_ms: int | None = None


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def align_sources(
    parsed: dict[str, ParsedSource],
    *,
    host_boot_time_utc: datetime | None,
    failed_at_utc_ms: int | None = None,
) -> AlignedContext:
    ctx = AlignedContext()
    ctx.failed_at_utc_ms = failed_at_utc_ms
    for source, ps in parsed.items():
        if source in {"test", "scheduler", "generic", "image"}:
            ctx.aligned[source] = source in {"test", "scheduler"}
        elif source == "dmesg":
            ctx.aligned[source] = host_boot_time_utc is not None and bool(ps.events)
        elif source == "uart":
            ctx.aligned[source] = False
            offset = _uart_offset(parsed)
            if offset is not None:
                ctx.offset_ms[source] = offset
                ctx.aligned[source] = True
    aligned_count = sum(1 for ok in ctx.aligned.values() if ok)
    ctx.alignment_rate = aligned_count / len(parsed) if parsed else 0.0
    _fill_times(ctx, parsed, host_boot_time_utc)
    return ctx


def _uart_offset(parsed: dict[str, ParsedSource]) -> int | None:
    test_ps = parsed.get("test")
    uart_ps = parsed.get("uart")
    if test_ps is None or uart_ps is None:
        return None
    cmd_tick = next(
        (e.tick for e in test_ps.events if e.tick is not None and e.ts_utc is not None),
        None,
    )
    uart_has_tick = (
        any(k.tick == cmd_tick for k in uart_ps.key_lines) if cmd_tick is not None else False
    )
    if cmd_tick is None or not uart_has_tick:
        return None
    cmd_ts = next(e.ts_utc for e in test_ps.events if e.tick == cmd_tick and e.ts_utc is not None)
    return _ms(cmd_ts) - cmd_tick  # tick 单位 1ms


def _fill_times(
    ctx: AlignedContext, parsed: dict[str, ParsedSource], boot: datetime | None
) -> None:
    for source, ps in parsed.items():
        times: dict[str, datetime] = {}
        if source == "uart":
            offset = ctx.offset_ms.get("uart")
            if offset is not None:
                last_anchor_ms: int | None = None
                for k in ps.key_lines:
                    if k.tick is not None:
                        last_anchor_ms = offset + k.tick
                        times[k.text] = datetime.fromtimestamp(last_anchor_ms / 1000, tz=UTC)
                    elif last_anchor_ms is not None:
                        # 无 tick 的关键行沿用最近一个 tick 锚点的绝对时间
                        times[k.text] = datetime.fromtimestamp(last_anchor_ms / 1000, tz=UTC)
        else:
            for k in ps.key_lines:
                if k.ts_utc is not None:
                    times[k.text] = k.ts_utc
        ctx.times[source] = times


def lock_anchor(ctx: AlignedContext, first_error: FirstError | None) -> Anchor:
    candidates: list[tuple[datetime, str]] = []
    for source, times in ctx.times.items():
        if source == "test":
            continue  # 首错单独参与
        for ts in times.values():
            candidates.append((ts, source))
    if first_error is not None:
        candidates.append((first_error.ts_utc, "test"))
    if not candidates:
        if ctx.failed_at_utc_ms is not None:
            return Anchor(
                ts_utc=datetime.fromtimestamp(ctx.failed_at_utc_ms / 1000, tz=UTC),
                source="test",
                cascaded=False,
            )
        raise ValueError("锚点无法确定：无任何时间证据")
    anchor_ts, source = min(candidates, key=lambda item: item[0])
    cascaded = source != "test" or (first_error is not None and anchor_ts < first_error.ts_utc)
    return Anchor(ts_utc=anchor_ts, source=source, cascaded=cascaded)
