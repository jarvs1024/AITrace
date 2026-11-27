#!/usr/bin/env bash
# deploy/scripts/install_release.sh
set -euo pipefail

root="${AITRACE_INSTALL_ROOT:-/opt/aitrace}"
src="${1:?usage: install_release.sh <release-dir> [--make-current]}"
make_current=0
[[ "${2:-}" == "--make-current" ]] && make_current=1

[[ -d "${src}" ]] || { echo "发布目录不存在：${src}" >&2; exit 1; }
[[ -f "${src}/REVISION" ]] || { echo "发布目录缺少 REVISION：${src}" >&2; exit 1; }

revision="$(cat "${src}/REVISION")"
target="${root}/releases/${revision}"

if [[ -e "${target}" ]]; then
  echo "目标版本目录已存在：${target}" >&2; exit 1
fi

mkdir -p "${root}/releases"
install -d -m 0755 "${target}"
cp -a "${src}/." "${target}/"

python3.13 -m venv "${target}/venv"
"${target}/venv/bin/pip" install --no-deps "${target}"/wheels/*.whl

cat > "${target}/alembic.ini" <<EOF
[alembic]
script_location = ${target}/venv/lib/python3.13/site-packages/aitrace/persistence/migrations

[loggers]
keys = root,sqlalchemy,alembic
[handlers]
keys = console
[formatters]
keys = generic
[logger_root]
level = WARNING
handlers = console
[logger_sqlalchemy]
level = WARNING
handlers =
[logger_alembic]
level = INFO
handlers =
[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic
[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
EOF

if [[ "${make_current}" == 1 ]]; then
  ln -sfn "${target}" "${root}/current"
  echo "安装完成并设为当前版本：${revision}"
else
  echo "安装完成：${revision}"
fi