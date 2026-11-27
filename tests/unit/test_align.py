from datetime import UTC, datetime

from aitrace.cleaning.align import AlignedContext, align_sources, lock_anchor
from aitrace.cleaning.registry import CleaningRegistry
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)
T0 = datetime(2026, 11, 20, 10, 53, 43, 500000, tzinfo=UTC)
CMD_TS = datetime(2026, 11, 20, 10, 0, 2, tzinfo=UTC)


def _ctx() -> AlignedContext:
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg"), boot_utc=BOOT),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    return align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)


def test_alignment_status() -> None:
    ctx = _ctx()
    assert ctx.aligned["test"] is True
    assert ctx.aligned["scheduler"] is True
    assert ctx.aligned["dmesg"] is True
    assert ctx.aligned["uart"] is True
    assert ctx.alignment_rate == 1.0


def test_uart_interlock_offset() -> None:
    ctx = _ctx()
    # tick 单位 1ms：uart tick 12345 ↔ test 10:00:02.000
    assert ctx.offset_ms["uart"] == int(CMD_TS.timestamp() * 1000) - 12345
    uart_ts = ctx.times["uart"]["ASSERT gpf phase2 timeout, reg0=0xDEAD reg1=0xBEEF"]
    assert uart_ts is not None


def test_dmesg_boot_relative_conversion() -> None:
    ctx = _ctx()
    aer_ts = ctx.times["dmesg"]["nvme nvme0: AER: uncorrected error received"]
    assert aer_ts == datetime(2026, 11, 20, 2, 5, 0, tzinfo=UTC)


def test_anchor_cascade_to_earliest_hard_error() -> None:
    ctx = _ctx()
    anchor = lock_anchor(ctx, None)
    # 最早硬异常 = dmesg AER @ 02:05:00（远早于 T0 10:53:43.5）
    assert anchor.ts_utc == datetime(2026, 11, 20, 2, 5, 0, tzinfo=UTC)
    assert anchor.cascaded is True


def test_anchor_falls_back_to_failed_at() -> None:
    parsed = {"test": CleaningRegistry().parse("test", "2026-11-20T10:00:00Z INFO ok\n")}
    ctx = align_sources(parsed, host_boot_time_utc=BOOT, failed_at_utc_ms=FAILED_AT)
    anchor = lock_anchor(ctx, None)
    assert anchor.ts_utc == datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC)
    assert anchor.cascaded is False


def test_missing_boot_time_marks_dmesg_unaligned() -> None:
    parsed = {"dmesg": CleaningRegistry().parse("dmesg", "[  300.000000] nvme AER\n")}
    ctx = align_sources(parsed, host_boot_time_utc=None, failed_at_utc_ms=FAILED_AT)
    assert ctx.aligned["dmesg"] is False
