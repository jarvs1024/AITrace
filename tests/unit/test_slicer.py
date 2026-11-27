from datetime import UTC, datetime

from aitrace.cleaning.align import align_sources, lock_anchor
from aitrace.cleaning.first_error import locate_first_error
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.slicer import build_slices
from aitrace.profiles.models import SliceParams
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)

PARAMS = SliceParams(
    baseline_lines=200,
    tail_lines=200,
    first_error_before_s=60,
    first_error_after_s=15,
    small_log_full_threshold_bytes=1_000_000,
    first_error_window_budget_bytes=5_000_000,
)


def _slices():
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg"), boot_utc=BOOT),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    return build_slices(parsed, ctx, anchor, PARAMS)


def test_small_logs_full_retained() -> None:
    slices = _slices()
    assert slices.full is True
    assert slices.shrinks == []


def test_error_window_covers_anchor_plus_minus() -> None:
    parsed = {"test": CleaningRegistry().parse("test", load_golden("test"))}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.full is True  # golden 测试日志 < 1MB 全量
    assert slices.baseline is None and slices.tail is None


def test_oversize_triggers_three_windows() -> None:
    big = "2026-11-20T10:53:43.500Z ERROR Traceback\n" + (
        "2026-11-20T10:53:43.501Z ERROR AssertionError: power down waiting gpf phase2 rsp, "
        "err_code=0x10B\n"
    )
    filler = "2026-11-20T10:00:00.000Z INFO padding line for size\n"
    text = filler * 100 + big * 8000 + filler * 100  # 超 1MB
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.full is False
    assert slices.baseline is not None and slices.tail is not None
    assert slices.error_window.size_bytes <= 5_000_000
    assert any("gpf" in line for line in slices.error_window.lines)


def test_window_shrink_order_and_exemptions() -> None:
    # 首错窗超 5MB：先缩前端基线→收尾→首错窗；必保行豁免保留
    head = "2026-11-20T10:00:00.000Z INFO head\n" * 299 + (
        "2026-11-20T10:00:00.000Z CMD nvme id-ctrl @CMD uart_tick=99999\n"
    )
    core = "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n" * 400_000  # > 5MB
    text = head + core
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert "baseline" in slices.shrinks or "error_window" in slices.shrinks
    assert slices.exemptions > 0
    assert "gpf" in slices.error_window.lines[0]


def test_must_keep_lines_outside_windows_are_exempted() -> None:
    # 必保行（ASSERT/exit code）落在窗口外也要进入首错窗
    filler = "2026-11-20T10:00:00.000Z INFO padding line for size\n"
    text = (
        filler * 20000
        # anchor 级联至最早 hard 错 10:20:00 → ASSERT 行落入窗口；43.500 行窗口外豁免必保
        + "2026-11-20T10:20:00.000Z ERROR ASSERT gpf timeout\n"
        + "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n"
    )
    parsed = {"test": CleaningRegistry().parse("test", text)}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    first = locate_first_error(parsed["test"].events)
    anchor = lock_anchor(ctx, first)
    slices = build_slices(parsed, ctx, anchor, PARAMS)
    assert slices.exemptions >= 1
    assert any("ASSERT gpf timeout" in line for line in slices.error_window.lines)
