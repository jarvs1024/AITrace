from datetime import UTC, datetime

from aitrace.cleaning.first_error import (
    FirstError,  # noqa: F401  # 冻结测试文件按 brief 保留该导入
    keyword_category,
    locate_first_error,
    relevant_sources,
)
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.types import ParsedSource, SourceKind
from tests.fixtures.logs import load_golden

T0 = datetime(2026, 11, 20, 10, 53, 43, 500000, tzinfo=UTC)


def test_locate_first_error_from_golden() -> None:
    parsed = CleaningRegistry().parse("test", load_golden("test"))
    first = locate_first_error(parsed.events)
    assert first is not None
    assert first.ts_utc == T0
    assert first.exc_type == "AssertionError"
    assert first.err_code == "0x10B"
    assert first.line_no == 6
    assert "gpf" in first.excerpt


def test_locate_returns_none_without_failure() -> None:
    parsed = ParsedSource(SourceKind.TEST, ("ok",))
    assert locate_first_error(parsed.events) is None


def test_err_code_from_exit_line() -> None:
    parsed = CleaningRegistry().parse("test", "2026-11-20T10:00:00Z FAILED exit code 42\n")
    first = locate_first_error(parsed.events)
    assert first is not None and first.err_code == "42"


def test_keyword_category_closed_set() -> None:
    assert keyword_category("power down waiting gpf") == "power"
    assert keyword_category("timeout waiting rsp") == "timeout"
    assert keyword_category("AER uncorrected error") == "pcie"
    assert keyword_category("protocol violation") == "protocol"
    assert keyword_category("ASSERT gpf") == "assert"
    assert keyword_category("I/O error on page") == "io"
    assert keyword_category("out of memory") == "memory"
    assert keyword_category("random unknown text") == "other"


def test_relevant_sources() -> None:
    parsed = {
        "test": CleaningRegistry().parse("test", load_golden("test")),
        "uart": CleaningRegistry().parse("uart", load_golden("uart")),
        "dmesg": CleaningRegistry().parse("dmesg", load_golden("dmesg")),
        "scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler")),
    }
    relevant = relevant_sources(parsed)
    assert relevant == {"test", "uart", "dmesg"}  # 调度日志无环境类异常 → 不相关
