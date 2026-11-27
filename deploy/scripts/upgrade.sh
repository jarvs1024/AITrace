#!/usr/bin/env bash
# deploy/scripts/upgrade.sh
set -euo pipefail
release="${1:?usage: upgrade.sh <release-dir>}"
previous="$(readlink -f /opt/aitrace/current)"
deploy/scripts/install_release.sh "${release}"
deploy/scripts/migrate.sh "/opt/aitrace/current"
systemctl restart aitrace.service
if ! deploy/scripts/healthcheck.sh; then
  echo "health check failed, rolling back" >&2
  deploy/scripts/rollback.sh "${previous}"
  exit 1
fi
