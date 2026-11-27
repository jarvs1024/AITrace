# src/aitrace/probe/whitelist.py
from __future__ import annotations

# 全局只读白名单（正则、全匹配）。这是探针命令的最终安全闸：
# 命令必须完全匹配其中一条，任何参数扩展都必须在模式内显式允许。
GLOBAL_PROBE_WHITELIST: tuple[str, ...] = (
    r"^nvme list$",
    r"^nvme id-ctrl( /dev/nvme[0-9]+(n[0-9]+)?)?$",
    r"^nvme smart-log( /dev/nvme[0-9]+(n[0-9]+)?)?( -x)?$",
    r"^nvme error-log( /dev/nvme[0-9]+(n[0-9]+)?)?( -p \d+)?$",
    r"^cxl list( -(v|U))*$",
    r"^lspci( -vvv| -vv| -v)?( -s [0-9a-fA-F:.]+)?$",
    r"^smartctl -a( /dev/(sd[a-z]+|nvme[0-9]+n[0-9]+))?$",
    r"^dmesg( --read)?$",
    r"^df -h( [/A-Za-z0-9_.-]+)?$",
    r"^ps -ef \| grep (nvme|cxl|fio|smartctl|dmesg|lspci|df)[A-Za-z0-9_.-]*$",
)

# 禁用模式（搜索匹配，先于全匹配白名单执行）：
# 写/复位/固件刷新等破坏性操作、以及 shell 元字符（分号/重定向/控制字符）
# 一律拒绝——白名单模式本身不允许这些字符，禁用模式提供更明确的拒绝原因。
DENY_PATTERNS: tuple[str, ...] = (
    r"(?i)\b(reboot|shutdown|poweroff|halt)\b",
    r"(?i)\bnvme\s+(format|fw-download|fw-commit|sanitize|reset|delete|write)\b",
    r"(?i)\b(rm|mkfs|dd|fdisk|parted)\b",
    r"[;&<>]",
    r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
)
