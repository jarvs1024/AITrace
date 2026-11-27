#!/usr/bin/env bash
# deploy/scripts/migrate.sh [--release] <release-dir>
set -euo pipefail
release="${1:?usage: migrate.sh [--release] <release-dir>}"
[[ "${release}" == "--release" ]] && release="${2:?usage: migrate.sh --release <release-dir>}"
[[ -f "${release}/alembic.ini" ]] || { echo "missing alembic.ini" >&2; exit 1; }
"${release}/venv/bin/alembic" -c "${release}/alembic.ini" upgrade head