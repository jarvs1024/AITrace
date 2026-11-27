from aitrace.cleaning.registry import CleaningRegistry
from tests.fixtures.logs import load_golden


def test_golden_test_log_layout() -> None:
    text = load_golden("test")
    assert "2026-11-20T10:53:43.500Z" in text  # T0 首错
    assert "@CMD uart_tick=12345" in text  # 串口互锁锚点
    assert "AssertionError" in text
    parsed = CleaningRegistry().parse("test", text)
    assert any(e.kind == "hard_error" for e in parsed.events)
    assert any(e.tick == 12345 for e in parsed.events)


def test_golden_uart_layout() -> None:
    text = load_golden("uart")
    assert "@TICK 12345" in text  # 互锁对端
    assert "ASSERT" in text
    assert "Build ID" in text
    parsed = CleaningRegistry().parse("uart", text)
    assert any(k.kind == "hard_error" for k in parsed.key_lines)


def test_golden_dmesg_layout() -> None:
    text = load_golden("dmesg")
    assert "[  300.000000]" in text  # AER 相对秒
    assert "AER" in text
    parsed = CleaningRegistry().parse("dmesg", text)
    assert any("AER" in k.text for k in parsed.key_lines)


def test_golden_scheduler_layout() -> None:
    text = load_golden("scheduler")
    assert "锁板冲突" in text
    parsed = CleaningRegistry().parse("scheduler", text)
    assert any("锁板冲突" in k.text for k in parsed.key_lines)
