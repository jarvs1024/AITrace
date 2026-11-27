# src/aitrace/profiles/loader.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from aitrace.profiles.models import DomainProfile


class ProfileConfigError(RuntimeError):
    pass


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    default: str
    domains: dict[str, str]


@dataclass(frozen=True)
class ProfileRegistry:
    default_profile: DomainProfile
    profiles: Mapping[str, DomainProfile]
    product_index: Mapping[str, str]


def load_profiles(domains_dir: Path) -> ProfileRegistry:
    registry_path = domains_dir / "registry.yaml"
    if not registry_path.is_file():
        raise ProfileConfigError(f"领域画像注册表不存在：{registry_path}")
    try:
        registry = RegistryFile.model_validate(
            yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        )
    except (ValidationError, yaml.YAMLError) as exc:
        raise ProfileConfigError(f"领域画像注册表校验失败：{exc}") from exc

    profiles: dict[str, DomainProfile] = {}
    for domain_id, filename in registry.domains.items():
        path = domains_dir / filename
        if not path.is_file():
            raise ProfileConfigError(f"领域画像文件不存在：{path}")
        try:
            profile = DomainProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as exc:
            raise ProfileConfigError(f"领域画像 {domain_id} 校验失败：{exc}") from exc
        if profile.domain_id != domain_id:
            raise ProfileConfigError(
                f"领域画像 id 不一致：注册键 {domain_id}，文件内 {profile.domain_id}"
            )
        profiles[domain_id] = profile

    if registry.default not in profiles:
        raise ProfileConfigError(f"默认画像 {registry.default} 未在 domains 中定义")

    product_index: dict[str, str] = {}
    for domain_id, profile in profiles.items():
        for product_id in profile.product_ids:
            if product_id in product_index:
                raise ProfileConfigError(
                    f"产品 {product_id} 重复归属领域：{product_index[product_id]} 与 {domain_id}"
                )
            product_index[product_id] = domain_id

    return ProfileRegistry(
        default_profile=profiles[registry.default],
        profiles=MappingProxyType(profiles),
        product_index=MappingProxyType(product_index),
    )
