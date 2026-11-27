# tests/unit/test_cutoff_rules.py
from pathlib import Path

from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet, build_rule_fault_info
from tests.fixtures.logs import load_golden

RULES_YAML = """
rules:
  - id: lock_conflict
    category: TEST_ENV_ERROR
    sources: [scheduler]
    pattern: "锁板冲突"
    detail: "锁板冲突：{line}"
  - id: pcie_link_down
    category: TEST_ENV_ERROR
    sources: [dmesg]
    pattern: "PCIe Link Down"
    detail: "PCIe 链路掉线：{line}"
"""


def _rules(tmp_path: Path) -> RuleSet:
    path = tmp_path / "cutoff.yaml"
    path.write_text(RULES_YAML, encoding="utf-8")
    return RuleSet.load(path)


def test_lock_conflict_rule_hits(tmp_path: Path) -> None:
    parsed = {"scheduler": CleaningRegistry().parse("scheduler", load_golden("scheduler"))}
    hit = _rules(tmp_path).evaluate(parsed)
    assert hit is not None
    assert hit.rule_id == "lock_conflict"
    assert hit.category == "TEST_ENV_ERROR"
    assert "锁板冲突" in hit.detail


def test_no_rule_hits_without_marker(tmp_path: Path) -> None:
    parsed = {
        "scheduler": CleaningRegistry().parse("scheduler", "2026-11-20T10:00:00Z STATE EXIT\n")
    }
    assert _rules(tmp_path).evaluate(parsed) is None


def test_rule_source_scoping(tmp_path: Path) -> None:
    # 规则限 scheduler 源：同样的 marker 出现在 test 源不命中
    parsed = {"test": CleaningRegistry().parse("test", "2026-11-20T10:00:00Z ERROR 锁板冲突\n")}
    assert _rules(tmp_path).evaluate(parsed) is None


def test_rule_config_missing_file_chinese_error(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="截流规则"):
        RuleSet.load(tmp_path / "nope.yaml")


def test_build_rule_fault_info_uses_authoritative_fields() -> None:
    info = build_rule_fault_info(
        {
            "product_id": "ssd_9560",
            "env_topology": {"host_ip": "10.20.68.114"},
            "hardware_type": "SSD",
        }
    )
    assert info["product"] == "ssd_9560"
    assert info["env_topology"]["host_ip"] == "10.20.68.114"


def test_shipped_cutoff_yaml_loads() -> None:
    path = Path("deploy/config/rules/cutoff.yaml")
    rules = RuleSet.load(path)
    assert len(rules.rules) >= 5  # 锁板冲突/盘被占用/Agent失联/任务取消/PCIe Link Down
