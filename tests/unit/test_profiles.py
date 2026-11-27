# tests/unit/test_profiles.py
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aitrace.profiles.loader import ProfileConfigError, load_profiles
from aitrace.profiles.models import DomainProfile
from aitrace.profiles.resolver import ProfileResolver, profile_version

DOMAINS = Path("deploy/config/domains")


def test_default_profile_is_loaded() -> None:
    registry = load_profiles(DOMAINS)
    assert registry.default_profile.domain_id == "default"
    assert registry.default_profile.root_cause_enum[:5] == (
        "TEST_ENV_ERROR",
        "FW_DEFECT",
        "SCRIPT_ISSUE",
        "HW_DEFECT",
        "OTHER",
    )


def test_product_resolves_to_its_domain() -> None:
    resolved = ProfileResolver(load_profiles(DOMAINS)).resolve("ssd_9560")
    assert resolved.domain_id == "enterprise"
    assert resolved.product_id == "ssd_9560"
    assert resolved.version == profile_version(resolved.config)


def test_unknown_product_falls_back_to_default() -> None:
    resolved = ProfileResolver(load_profiles(DOMAINS)).resolve("unknown_product")
    assert resolved.domain_id == "default"


def test_duplicate_product_across_domains_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "default.yaml").write_text(DOMAINS.joinpath("default.yaml").read_text())
    for name in ("enterprise", "consumer", "cxl"):
        (tmp_path / f"{name}.yaml").write_text(DOMAINS.joinpath(f"{name}.yaml").read_text())
    data = yaml.safe_load((DOMAINS / "consumer.yaml").read_text())
    data["product_ids"] = ["ssd_9560"]
    (tmp_path / "consumer.yaml").write_text(yaml.safe_dump(data, allow_unicode=True))
    (tmp_path / "registry.yaml").write_text((DOMAINS / "registry.yaml").read_text())
    with pytest.raises(ProfileConfigError) as caught:
        load_profiles(tmp_path)
    assert "重复" in str(caught.value)


def test_write_command_is_rejected_from_probe_whitelist() -> None:
    data = yaml.safe_load((DOMAINS / "enterprise.yaml").read_text())
    data["probe_whitelist"] = ["nvme format /dev/nvme0"]
    with pytest.raises(ValidationError) as caught:
        DomainProfile.model_validate(data)
    assert "只读" in str(caught.value)


def test_confidence_weights_must_sum_to_one() -> None:
    data = yaml.safe_load((DOMAINS / "enterprise.yaml").read_text())
    data["confidence_weights"]["reference_hit"] = 0.5
    with pytest.raises(ValidationError) as caught:
        DomainProfile.model_validate(data)
    assert "1.0" in str(caught.value)


def test_version_changes_when_profile_changes() -> None:
    base = DomainProfile.model_validate(yaml.safe_load((DOMAINS / "enterprise.yaml").read_text()))
    changed = base.model_copy(update={"analysis_focus": "不同的分析侧重"})
    assert profile_version(base) != profile_version(changed)
