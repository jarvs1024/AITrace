# src/aitrace/probe/assets.py
from __future__ import annotations

import ipaddress
from collections.abc import Sequence


def host_in_assets(host: str, allowlist: Sequence[str], *, allow_loopback: bool) -> bool:
    """SSH 探针目标主机资产闸：仅放行回环与 allowlist 内 IP。

    host 必须为合法 IP（IPv4/IPv6）；主机名一律拒绝——投递契约
    EnvTopology.host_ip 已按 IPv4 校验，探针不信任名称解析。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return allow_loopback
    for entry in allowlist:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        try:
            in_network = ip in network
        except TypeError:
            # IP 与网段版本不一致（如 IPv4 地址对 IPv6 网段）时成员判定无意义
            in_network = False
        if in_network:
            return True
    return False
