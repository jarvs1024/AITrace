from datetime import UTC, datetime

import pytest

from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.types import SourceKind

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)


def test_testlog_strips_ansi_and_parses_events() -> None:
    text = (
        "\x1b[32m2026-11-20T10:00:01.123Z INFO device ready\r\n"
        "2026-11-20T10:00:02.000Z CMD nvme id-ctrl @CMD uart_tick=12345\n"
        "2026-11-20T10:53:43.500Z ERROR Traceback (most recent call last):\n"
        "2026-11-20T10:53:43.501Z ERROR AssertionError: power down waiting gpf\n"
        "2026-11-20T10:53:44.000Z FAILED exit code 42\n"
    )
    parsed = CleaningRegistry().parse("test", text)
    assert parsed.source == SourceKind.TEST
    assert all("\x1b" not in e.text for e in parsed.events)
    assert len(parsed.events) == 5
    kinds = {e.kind for e in parsed.events}
    assert "hard_error" in kinds and "command" in kinds
    assert parsed.events[1].tick == 12345
    assert parsed.events[1].ts_utc == datetime(2026, 11, 20, 10, 0, 2, tzinfo=UTC)
    assert parsed.key_lines and any("exit code" in k.text for k in parsed.key_lines)


def test_uart_folds_heartbeat_and_keeps_assert_context() -> None:
    lines = ["\x00\x01garbage\xff", "heartbeat 1", "heartbeat 2", "heartbeat 3"]
    lines += [f"ASSERT ctx {i}" for i in range(31)]
    text = "\n".join(lines)
    parsed = CleaningRegistry().parse("uart", text)
    assert "garbage" in parsed.raw_lines[0]
    assert parsed.metrics["heartbeat_folded"] == 2  # 3 行折叠为 1 行，净省 2
    assert len(parsed.key_lines) >= 1
    assert parsed.key_lines[0].kind == "hard_error"
    assert len(parsed.events) > 0
    assert all(e.ts_utc is None for e in parsed.events)  # 串口无时钟


def test_uart_extracts_tick_and_build_id() -> None:
    text = "Build ID: FW-20261120-001\n@TICK 12345\ncmd power down\n"
    parsed = CleaningRegistry().parse("uart", text)
    ticks = [e.tick for e in parsed.events if e.tick is not None]
    assert 12345 in ticks
    assert any("FW-20261120-001" in k.text for k in parsed.key_lines)


def test_dmesg_whitelist_and_folds_repeats() -> None:
    text = (
        "[    0.000000] Booting Linux\n"
        "[   12.345678] nvme nvme0: AER: corrected error received\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   12.500000] usb 1-1: device descriptor read/64, error -71\n"
        "[   13.000000] nvme0: I/O tag 42 opcode 0x2 status: 0x400\n"
    )
    parsed = CleaningRegistry().parse("dmesg", text, boot_utc=BOOT)
    # usb 不在硬件白名单 → 折叠但不进事件流
    assert all(e.text.startswith(("nvme", "Booting")) for e in parsed.events)
    assert parsed.metrics["repeats_folded"] == 2
    aer = next(e for e in parsed.events if "AER" in e.text)
    assert aer.ts_utc == datetime(2026, 11, 20, 2, 0, 12, 345678, tzinfo=UTC)
    assert any(k.kind == "hard_error" for k in parsed.key_lines)


def test_dmesg_call_trace_is_hard_error() -> None:
    text = (
        "[   20.000000] BUG: unable to handle kernel NULL pointer\n"
        "[   20.000001] Call Trace:\n"
        "[   20.000002]  nvme_queue_rq+0x1a/0x20\n"
    )
    parsed = CleaningRegistry().parse("dmesg", text, boot_utc=BOOT)
    assert any("Call Trace" in k.text for k in parsed.key_lines)


def test_scheduler_compresses_states_and_flags_lock_conflict() -> None:
    text = (
        "2026-11-20T10:00:00.000Z STATE WAIT_ENV\n"
        "2026-11-20T10:00:01.000Z STATE WAIT_ENV\n"
        "2026-11-20T10:05:00.000Z STATE LOCK_DEVICE\n"
        "2026-11-20T10:05:01.000Z ERROR 锁板冲突：device busy\n"
        "2026-11-20T10:06:00.000Z STATE EXIT\n"
    )
    parsed = CleaningRegistry().parse("scheduler", text)
    assert parsed.metrics["states_compressed"] >= 1
    assert any("锁板冲突" in k.text for k in parsed.key_lines)
    assert parsed.events and all(e.ts_utc is not None for e in parsed.events)


def test_generic_strips_binary_and_limits_length() -> None:
    text = "\x00\x01hello\xff" * 100_000  # 60 万字符，超 20 万上限
    parsed = CleaningRegistry().parse("generic", text)
    assert len("".join(parsed.raw_lines)) <= 200_000
    assert "\x00" not in parsed.raw_lines[0]
    assert parsed.metrics["truncated"] is True


def test_image_passthrough() -> None:
    parsed = CleaningRegistry().parse("image", "<binary>")
    assert parsed.events == ()
    assert parsed.metrics["passthrough"] is True


def test_unknown_source_raises_chinese_error() -> None:
    with pytest.raises(ValueError, match="日志源"):
        CleaningRegistry().parse("nope", "x")
