# tests/unit/test_fingerprint.py
from aitrace.cleaning.fingerprint import (
    dedup_key_lines,
    exact_fingerprint,
    stable_fingerprint,
)
from aitrace.cleaning.types import KeyLine


def test_exact_fingerprint_components_and_length() -> None:
    fp = exact_fingerprint("/cases/power.py", 88, "AssertionError", "0x10B", "SN-001")
    assert len(fp) == 16
    same = exact_fingerprint("/cases/power.py", 88, "AssertionError", "0x10B", "SN-001")
    assert fp == same
    moved = exact_fingerprint("/cases/power.py", 89, "AssertionError", "0x10B", "SN-001")
    assert fp != moved  # 行号漂移 → 精确指纹变化


def test_stable_fingerprint_immune_to_path_and_line() -> None:
    fp = stable_fingerprint("AssertionError", "0x10B", "SSD", "power")
    again = stable_fingerprint("AssertionError", "0x10B", "SSD", "power")
    assert fp == again and len(fp) == 16
    other = stable_fingerprint("AssertionError", "0x10B", "CXL", "power")
    assert fp != other  # 硬件域特征参与


def test_dedup_merges_same_fingerprint_with_count_and_range() -> None:
    lines = (
        KeyLine(10, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(11, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(12, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(13, "hard_error", "gpf timeout err_code=0x10B"),
        KeyLine(20, "hard_error", "AER link down err_code=0x02"),
    )

    def exact(line: KeyLine) -> str:
        return exact_fingerprint(
            "/cases/power.py", line.line_no // 10, "AssertionError", "0x10B", "SN-001"
        )

    groups = dedup_key_lines(lines, exact)
    by_fp = {g.exact_fp: g for g in groups}
    assert len(groups) == 2
    gpf = by_fp[exact(lines[0])]
    assert gpf.count == 4
    assert gpf.first_line == 10 and gpf.last_line == 13


def test_dedup_never_crosses_case_boundary() -> None:
    # 同一函数对两个用例的 key_lines 分别调用，绝不合并（契约测试锁定调用方式）
    from aitrace.cleaning.fingerprint import exact_fingerprint as exact

    def lines_of(case_id: str) -> tuple[KeyLine, ...]:
        return (KeyLine(1, "hard_error", f"{case_id} gpf err_code=0x10B"),)

    groups_a = dedup_key_lines(lines_of("C1"), lambda k: exact("a.py", k.line_no, "E", "1", "SN-1"))
    groups_b = dedup_key_lines(lines_of("C2"), lambda k: exact("b.py", k.line_no, "E", "1", "SN-2"))
    assert groups_a[0].exact_fp != groups_b[0].exact_fp
    assert groups_a[0].count == 1 and groups_b[0].count == 1
