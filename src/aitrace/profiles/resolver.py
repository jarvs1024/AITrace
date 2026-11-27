# src/aitrace/profiles/resolver.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from aitrace.profiles.loader import ProfileRegistry
from aitrace.profiles.models import DomainProfile


@dataclass(frozen=True)
class ResolvedProfile:
    domain_id: str
    version: str
    config: DomainProfile
    product_id: str | None


def profile_version(profile: DomainProfile) -> str:
    payload = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ProfileResolver:
    def __init__(self, registry: ProfileRegistry) -> None:
        self._registry = registry

    def resolve(self, product_id: str | None) -> ResolvedProfile:
        domain_id = self._registry.product_index.get(
            product_id or "", self._registry.default_profile.domain_id
        )
        profile = self._registry.profiles[domain_id]
        return ResolvedProfile(
            domain_id=domain_id,
            version=profile_version(profile),
            config=profile,
            product_id=product_id,
        )
