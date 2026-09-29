#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
GRID_NAME=${GRID_NAME:-r31_fullrun_freeze_9plus1}
LATEST="$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME/latest_campaign"
[ -s "$LATEST" ] || { echo "No campaign yet: $LATEST" >&2; exit 1; }
CAMPAIGN=$(cat "$LATEST")
PY=${PYTHON:-$ENV/bin/python}; [ -x "$PY" ] || PY=$(command -v python3)
exec "$PY" "$ROOT/tools/render_v31_dashboard.py" "$CAMPAIGN"
