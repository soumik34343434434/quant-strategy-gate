#!/usr/bin/env bash
# deploy.sh <strategy_dir> <report_dir>
# Refuses unless a fresh check of this exact strategy version is DEPLOYABLE.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -ne 2 ]; then
  echo "usage: deploy.sh <strategy_dir> <report_dir>    (there are no options)" >&2
  exit 2
fi
for arg in "$@"; do
  case "$arg" in -*) echo "usage: deploy.sh <strategy_dir> <report_dir>    (there are no options)" >&2; exit 2 ;; esac
done

PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" -I "$HERE/deploy.py" deploy "$1" "$2"
