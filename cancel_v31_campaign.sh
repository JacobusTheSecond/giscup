#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
GRID_NAME=${GRID_NAME:-r31_fullrun_freeze_9plus1}
LATEST="$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME/latest_campaign"
[ -s "$LATEST" ] || { echo "No campaign pointer found: $LATEST"; exit 0; }
CAMPAIGN=$(cat "$LATEST")
touch "$CAMPAIGN/STOP"
echo "STOP marker written: $CAMPAIGN/STOP"
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
[ -s "$CAMPAIGN/jobs.tsv" ] && awk -F'\t' 'NR>1 && $6 ~ /^[0-9]+$/ {print $6}' "$CAMPAIGN/jobs.tsv" >> "$TMP"
[ -s "$CAMPAIGN/edge_foundry_job.tsv" ] && awk -F'\t' 'NR>1 && $2 ~ /^[0-9]+$/ {print $2}' "$CAMPAIGN/edge_foundry_job.tsv" >> "$TMP"
[ -s "$CAMPAIGN/edge_foundry_history.tsv" ] && awk -F'\t' 'NR>1 && $2 ~ /^[0-9]+$/ {print $2}' "$CAMPAIGN/edge_foundry_history.tsv" >> "$TMP"
# Safety net: only RINGARC31 jobs belonging to this user.
squeue -h -u "$USER" -o '%i|%j' 2>/dev/null | awk -F'|' '$2 ~ /^r31-/ {print $1}' >> "$TMP" || true
mapfile -t JOBS < <(sort -u "$TMP" | grep -E '^[0-9]+$' || true)
[ "${#JOBS[@]}" -eq 0 ] || scancel "${JOBS[@]}" || true
if [ -s "$CAMPAIGN/tmux_session.txt" ]; then
  SESSION=$(cat "$CAMPAIGN/tmux_session.txt")
  tmux kill-session -t "$SESSION" 2>/dev/null || true
fi
if [ -s "$CAMPAIGN/classic_tmux_session.txt" ]; then
  CLASSIC_SESSION=$(cat "$CAMPAIGN/classic_tmux_session.txt")
  tmux kill-session -t "$CLASSIC_SESSION" 2>/dev/null || true
fi
echo "Cancellation requested. Check: squeue -u $USER"
