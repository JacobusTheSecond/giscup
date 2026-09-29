#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
CAMPAIGN=${1:-}
if [ -z "$CAMPAIGN" ]; then
  echo "usage: $0 CAMPAIGN_DIR" >&2
  exit 2
fi
CAMPAIGN=$(realpath -e "$CAMPAIGN")
PY=${PYTHON:-$ENV/bin/python}
[ -x "$PY" ] || PY=python3
exec "$PY" "$ROOT/summarize_ringarc31_campaign.py" --campaign "$CAMPAIGN"
