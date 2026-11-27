#!/usr/bin/env bash
# deploy/scripts/upgrade.sh [--skip-backup-check] <release-dir>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AITRACE_INSTALL_ROOT:-/opt/aitrace}"
SERVICE="${AITRACE_SERVICE_NAME:-aitrace.service}"
LOG_FILE="${AITRACE_UPGRADE_LOG:-/var/log/aitrace/upgrade.log}"
RETRIES="${AITRACE_READYZ_RETRIES:-60}"
INTERVAL="${AITRACE_READYZ_INTERVAL:-2}"
STOP_WAIT="${AITRACE_STOP_WAIT:-30}"
export AITRACE_READYZ_RETRIES="${RETRIES}"
export AITRACE_READYZ_INTERVAL="${INTERVAL}"

_log() {
  local line="$*"
  echo "${line}" >&2
  if [[ -n "${LOG_FILE}" ]]; then
    local dir
    dir="$(dirname "${LOG_FILE}")"
    if [[ -w "${LOG_FILE}" ]] || { mkdir -p "${dir}" 2>/dev/null && [[ -w "${dir}" ]]; }; then
      echo "${line}" >> "${LOG_FILE}"
    fi
  fi
}

fail() {
  _log "升级失败：$1"
  exit 1
}

resolve_current() {
  local link="${1}"
  [[ -L "${link}" ]] || return 1
  local target
  target="$(readlink "${link}")" || return 1
  [[ "${target}" == /* ]] && echo "${target}" || echo "${link%/*}/${target}"
}

skip_backup_check=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --skip-backup-check) skip_backup_check=1; shift ;;
    *) break ;;
  esac
done

src="${1:?usage: upgrade.sh [--skip-backup-check] <release-dir>}"

[[ -d "${src}" ]] || fail "发布目录不存在：${src}"
[[ -f "${src}/REVISION" ]] || fail "发布目录缺少 REVISION：${src}"
[[ -f "${src}/DB_COMPAT" ]] || fail "发布目录缺少 DB_COMPAT：${src}"

revision="$(cat "${src}/REVISION")"
target="${ROOT}/releases/${revision}"
previous="$(resolve_current "${ROOT}/current")" || fail "无法解析当前版本（${ROOT}/current 不是符号链接或不存在）"
_log "预检通过：当前 ${previous} → 目标 ${target}"

if [[ "${skip_backup_check}" != 1 ]]; then
  staging="${AITRACE_BACKUP__STAGING_DIR:-/var/lib/aitrace/backups}"
  newest="$(find "${staging}" -maxdepth 1 -name 'aitrace-*.dump' -type f -mtime -1 -print -quit 2>/dev/null || true)"
  [[ -n "${newest}" ]] || fail "未发现 24 小时内的数据库备份；请先执行 aitrace-backup，或确认无状态变更后加 --skip-backup-check 重试"
fi

if ! "${SCRIPT_DIR}/healthcheck.sh"; then
  fail "当前服务未通过 readyz 探活（${SERVICE}），中止升级；请先排查现网状态"
fi

"${SCRIPT_DIR}/install_release.sh" "${src}"
_log "安装完成：${target}"

systemctl stop "${SERVICE}"
_log "服务已停止：${SERVICE}"
for ((i = 0; i < STOP_WAIT; i++)); do
  if "${SCRIPT_DIR}/healthcheck.sh" 2>/dev/null; then sleep 1; else break; fi
done
if "${SCRIPT_DIR}/healthcheck.sh" 2>/dev/null; then
  fail "服务未能在超时内优雅退出：${SERVICE}"
fi

"${SCRIPT_DIR}/migrate.sh" "${target}"
_log "迁移完成：${target}"

ln -sfn "${target}" "${ROOT}/current"
_log "切换 current：${target}"

systemctl start "${SERVICE}"
_log "服务已启动：${SERVICE}"
if "${SCRIPT_DIR}/healthcheck.sh" --retry "${RETRIES}"; then
  _log "readyz 通过：${SERVICE}"
else
  _log "升级失败，已回滚：${SERVICE} 启动后未通过 readyz"
  "${SCRIPT_DIR}/rollback.sh" "${previous}"
  exit 1
fi

while IFS= read -r name; do
  all+=("${name}")
done < <(ls -1 "${ROOT}/releases" | sort)
count="${#all[@]}"
drop=$((count - 3))
i=0
while ((i < drop)); do
  rm -rf "${ROOT}/releases/${all[i]}"
  _log "清理旧版本：${all[i]}"
  i=$((i + 1))
done