#!/usr/bin/env bash
# deploy/scripts/rollback.sh <release-dir>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AITRACE_INSTALL_ROOT:-/opt/aitrace}"
SERVICE="${AITRACE_SERVICE_NAME:-aitrace.service}"
LOG_FILE="${AITRACE_UPGRADE_LOG:-/var/log/aitrace/upgrade.log}"
STOP_WAIT="${AITRACE_STOP_WAIT:-30}"
export AITRACE_READYZ_RETRIES="${AITRACE_READYZ_RETRIES:-120}"
export AITRACE_READYZ_INTERVAL="${AITRACE_READYZ_INTERVAL:-2}"

target="${1:?usage: rollback.sh <release-dir>}"

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
  _log "回滚失败：$1"
  exit 1
}

[[ -d "${target}" ]] || fail "目标版本目录不存在：${target}"

systemctl stop "${SERVICE}"
_log "服务已停止：${SERVICE}"
for ((i = 0; i < STOP_WAIT; i++)); do
  if "${SCRIPT_DIR}/healthcheck.sh" 2>/dev/null; then sleep 1; else break; fi
done
if "${SCRIPT_DIR}/healthcheck.sh" 2>/dev/null; then
  fail "服务未能在超时内优雅退出：${SERVICE}"
fi

ln -sfn "${target}" "${ROOT}/current"
_log "切换 current：${target}"

systemctl start "${SERVICE}"
_log "服务已启动：${SERVICE}"
if "${SCRIPT_DIR}/healthcheck.sh" --retry "${AITRACE_READYZ_RETRIES:-120}"; then
  _log "回滚完成：${target}"
else
  fail "服务启动后未通过 readyz（${SERVICE}），请人工介入"
fi