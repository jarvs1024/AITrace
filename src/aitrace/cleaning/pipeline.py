from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aitrace.cleaning.align import AlignedContext, Anchor, align_sources, lock_anchor
from aitrace.cleaning.fingerprint import dedup_key_lines, exact_fingerprint, stable_fingerprint
from aitrace.cleaning.first_error import (
    FirstError,
    keyword_category,
    locate_first_error,
    relevant_sources,
)
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleHit, RuleSet
from aitrace.cleaning.slicer import Slices, build_slices
from aitrace.cleaning.types import ParsedSource
from aitrace.profiles.models import SliceParams
from aitrace.profiles.resolver import ResolvedProfile

_LOG_FIELDS = (
    ("test", "test_log_path"),
    ("uart", "uart_log_path"),
    ("dmesg", "kernel_log_path"),
    ("scheduler", "scheduler_log_path"),
)


@dataclass
class PipelineOutcome:
    status: str  # ready / rule_hit / failed
    first_error: FirstError | None = None
    anchor: Anchor | None = None
    stable_fp: str | None = None
    exact_fp: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    slices: Any = None
    rule_hit: RuleHit | None = None
    referenced_logs: list[dict[str, Any]] = field(default_factory=list)
    dedup_groups: tuple[Any, ...] = ()
    error: dict[str, Any] | None = None


def run_pipeline(
    *,
    payload: dict[str, Any],
    authoritative: dict[str, Any],
    raw_root: Path,
    profile: ResolvedProfile | None,
    rules: RuleSet,
    registry: CleaningRegistry,
) -> PipelineOutcome:
    started = datetime.now(UTC)
    parsed: dict[str, ParsedSource] = {}
    availability: dict[str, bool] = {}
    for source, field_name in _LOG_FIELDS:
        path = (payload.get("logs") or {}).get(field_name)
        if not path:
            availability[source] = False
            continue
        full = raw_root / path
        if not full.is_file():
            availability[source] = False
            continue
        availability[source] = True
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return PipelineOutcome(
                status="failed",
                error={
                    "code": "LOG_READ_FAILED",
                    "message": f"日志读取失败：{source}（{field_name}）路径 {path}；原因 {exc}",
                },
            )
        parsed[source] = registry.parse(source, text, boot_utc=_boot(authoritative))
    if not any(availability.values()):
        return PipelineOutcome(
            status="failed",
            error={
                "code": "NO_LOGS",
                "message": "清洗失败：投递的四路日志全部缺失或不可读，无法定位首错",
                "retryable": True,
            },
        )
    hit = rules.evaluate(parsed)
    failed_at_ms = int(payload["failed_at_utc"])
    ctx = _ctx(parsed, authoritative, failed_at_ms)
    first: FirstError | None = None
    test_ps = parsed.get("test")
    if test_ps is not None:
        first = locate_first_error(test_ps.events)
    device_id = (payload.get("env_binding") or {}).get("device_id", "")
    exc_type = first.exc_type if first else None
    err_code = first.err_code if first else None
    category = keyword_category(first.excerpt) if first else "other"
    if hit is not None:
        anchor = _anchor_or_failed(ctx, first, failed_at_ms)
        slices = build_slices(parsed, ctx, anchor, _params(profile))
        return PipelineOutcome(
            status="rule_hit",
            first_error=first,
            anchor=anchor,
            stable_fp=stable_fingerprint(
                exc_type, err_code, authoritative.get("hardware_type", "SSD"), category
            ),
            exact_fp=exact_fingerprint(
                str(payload.get("script_path", "")),
                first.line_no if first else 0,
                exc_type,
                err_code,
                device_id,
            ),
            summary=_summary("rule_hit", parsed, availability, first, anchor),
            metrics=_metrics(ctx, parsed, slices, started),
            slices=slices,
            rule_hit=hit,
        )
    anchor = _anchor_or_failed(ctx, first, failed_at_ms)
    slices = build_slices(parsed, ctx, anchor, _params(profile))
    exact = exact_fingerprint(
        str(payload.get("script_path", "")),
        first.line_no if first else 0,
        exc_type,
        err_code,
        device_id,
    )
    stable = stable_fingerprint(
        exc_type, err_code, authoritative.get("hardware_type", "SSD"), category
    )
    groups = dedup_key_lines(
        tuple(k for ps in parsed.values() for k in ps.key_lines if k.kind == "hard_error"),
        lambda k: exact_fingerprint(
            str(payload.get("script_path", "")), k.line_no, exc_type, err_code, device_id
        ),
    )
    return PipelineOutcome(
        status="ready",
        first_error=first,
        anchor=anchor,
        stable_fp=stable,
        exact_fp=exact,
        summary=_summary("ready", parsed, availability, first, anchor),
        metrics=_metrics(ctx, parsed, slices, started),
        slices=slices,
        referenced_logs=[],
        dedup_groups=groups,
    )


def _boot(authoritative: dict[str, Any]) -> datetime | None:
    boot_s = (authoritative.get("env_topology") or {}).get("host_boot_time_utc")
    return datetime.fromtimestamp(boot_s, tz=UTC) if boot_s else None


def _ctx(
    parsed: dict[str, ParsedSource], authoritative: dict[str, Any], failed_at_ms: int
) -> AlignedContext:
    return align_sources(
        parsed, host_boot_time_utc=_boot(authoritative), failed_at_utc_ms=failed_at_ms
    )


def _anchor_or_failed(ctx: AlignedContext, first: FirstError | None, failed_at_ms: int) -> Anchor:
    probe = first or FirstError(
        datetime.fromtimestamp(failed_at_ms / 1000, tz=UTC), None, None, 0, ""
    )
    try:
        return lock_anchor(ctx, probe)
    except ValueError:
        return Anchor(
            ts_utc=datetime.fromtimestamp(failed_at_ms / 1000, tz=UTC),
            source="fallback",
            cascaded=False,
        )


def _params(profile: ResolvedProfile | None) -> SliceParams:
    if profile is not None:
        return profile.config.slice_params
    return SliceParams()


def _summary(
    status: str,
    parsed: dict[str, ParsedSource],
    availability: dict[str, bool],
    first: FirstError | None,
    anchor: Anchor | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "source_availability": availability,
        "first_error": None
        if first is None
        else {
            "ts_utc": first.ts_utc.isoformat(),
            "exc_type": first.exc_type,
            "err_code": first.err_code,
            "line_no": first.line_no,
            "excerpt": first.excerpt,
        },
        "anchor_utc": None if anchor is None else anchor.ts_utc.isoformat(),
        "anchor_source": None if anchor is None else anchor.source,
        "relevant_sources": sorted(relevant_sources(parsed)),
    }


def _metrics(
    ctx: AlignedContext,
    parsed: dict[str, ParsedSource],
    slices: Slices,
    started: datetime,
) -> dict[str, Any]:
    per_source: dict[str, dict[str, Any]] = {}
    for source, ps in parsed.items():
        original = sum(len(line.encode("utf-8")) + 1 for line in ps.raw_lines)
        slice_bytes = original if slices.full else 0
        per_source[source] = {
            "original_bytes": original,
            "slice_bytes": slice_bytes,
            "events_parsed": len(ps.events),
        }
    return {
        "duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
        "per_source": per_source,
        "alignment_rate": ctx.alignment_rate,
        "shrinks": len(slices.shrinks),
        "exemptions": slices.exemptions,
    }
