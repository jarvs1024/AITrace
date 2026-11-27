from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aitrace.cleaning.types import ParsedSource


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    category: str
    detail: str
    source: str
    line: str


class RuleSet:
    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules

    @classmethod
    def load(cls, path: Path) -> RuleSet:
        if not path.is_file():
            raise ValueError(f"截流规则文件缺失：{path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules = data.get("rules", [])
        for rule in rules:
            for key in ("id", "category", "sources", "pattern"):
                if key not in rule:
                    raise ValueError(f"截流规则配置非法：规则缺少字段 {key}；文件 {path}")
        return cls(rules)

    def evaluate(self, parsed: dict[str, ParsedSource]) -> RuleHit | None:
        for rule in self.rules:
            pattern = re.compile(rule["pattern"])
            for source in rule["sources"]:
                ps = parsed.get(source)
                if ps is None:
                    continue
                for key in ps.key_lines:
                    m = pattern.search(key.text)
                    if m:
                        return RuleHit(
                            rule_id=rule["id"],
                            category=rule["category"],
                            detail=rule["detail"].format(line=key.text.strip()),
                            source=source,
                            line=key.text,
                        )
        return None


def build_rule_fault_info(authoritative: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": authoritative.get("product_id", "-"),
        "symptom": "",
        "fw_version": (
            authoritative.get("env_topology", {}).get("devices", [{}])[0].get("fw_version", "-")
            if authoritative.get("env_topology", {}).get("devices")
            else "-"
        ),
        "test_case": "-",
        "expected": "-",
        "actual": "-",
        "env_topology": {"host_ip": authoritative.get("env_topology", {}).get("host_ip", "-")},
    }
