#!/usr/bin/env bash
# deploy/scripts/install_release.sh
set -euo pipefail
src="${1:?usage: install_release.sh <release-dir>}"
revision="$(cat "${src}/REVISION")"
target="/opt/aitrace/releases/${revision}"
[[ -f "${src}/DB_COMPAT" ]] || { echo "missing DB_COMPAT" >&2; exit 1; }
install -d -m 0755 "${target}"
cp -a "${src}/." "${target}/"
python3.13 -m venv "${target}/venv"
"${target}/venv/bin/pip" install --upgrade pip
"${target}/venv/bin/pip" install --no-index --find-links "${target}/wheels" "aitrace==${revision}"
ln -sfn "${target}" /opt/aitrace/current
systemctl daemon-reload
systemctl enable aitrace.service
