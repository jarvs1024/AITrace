#!/usr/bin/env bash
# deploy/scripts/migrate.sh
set -euo pipefail
release="${1:?usage: migrate.sh <release-dir>}"
[[ -f "${release}/alembic.ini" ]] || { echo "missing alembic.ini" >&2; exit 1; }
"${release}/venv/bin/alembic" -c "${release}/alembic.ini" upgrade head
