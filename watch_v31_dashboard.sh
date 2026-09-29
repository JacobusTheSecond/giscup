#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
GRID_NAME=${GRID_NAME:-r31_fullrun_freeze_9plus1}
REFRESH=${GRID_DASHBOARD_REFRESH_SECONDS:-10}
LATEST="$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME/latest_campaign"
[ -s "$LATEST" ] || { echo "No campaign yet: $LATEST" >&2; exit 1; }
CAMPAIGN=$(cat "$LATEST")
SESSION_FILE="$CAMPAIGN/tmux_session.txt"
if [ -s "$SESSION_FILE" ]; then
  SESSION=$(cat "$SESSION_FILE")
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux select-window -t "$SESSION:dashboard" 2>/dev/null || true
    exec tmux attach -t "$SESSION"
  fi
fi
PY=${PYTHON:-$ENV/bin/python}; [ -x "$PY" ] || PY=$(command -v python3)
echo "Campaign tmux is unavailable; showing a foreground file-only dashboard."
exec "$PY" "$ROOT/tools/render_v31_dashboard.py" "$CAMPAIGN" --watch "$REFRESH"
