#!/usr/bin/env bash
# deploy/scripts/rollback.sh
set -euo pipefail
target="${1:?usage: rollback.sh <release-dir>}"
[[ -d "${target}" ]] || { echo "release not found: ${target}" >&2; exit 1; }
ln -sfn "${target}" /opt/aitrace/current
systemctl restart aitrace.service
