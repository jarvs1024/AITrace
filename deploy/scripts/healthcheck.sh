#!/usr/bin/env bash
# deploy/scripts/healthcheck.sh
set -euo pipefail
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/v1/readyz >/dev/null