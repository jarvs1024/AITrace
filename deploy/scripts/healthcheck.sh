#!/usr/bin/env bash
# deploy/scripts/healthcheck.sh [--retry [N]]
set -euo pipefail

url="${AITRACE_HEALTHCHECK_URL:-http://127.0.0.1:8080/api/v1/readyz}"
max_time="${AITRACE_HEALTHCHECK_MAX_TIME:-5}"
retries="${AITRACE_READYZ_RETRIES:-1}"
interval="${AITRACE_READYZ_INTERVAL:-2}"

if [[ "${1:-}" == "--retry" ]]; then
  retries="${2:-${AITRACE_READYZ_RETRIES:-60}}"
fi

for ((i = 0; i < retries; i++)); do
  if curl --fail --silent --show-error --max-time "${max_time}" "${url}" >/dev/null 2>&1; then
    exit 0
  fi
  [[ "${interval}" == "0" ]] || sleep "${interval}"
done
exit 1