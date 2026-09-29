#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
GRID_NAME=${GRID_NAME:-r31_fullrun_freeze_9plus1}
LATEST=$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME/latest_campaign
STARTUP_REFRESH=${GRID_CLASSIC_STARTUP_REFRESH_SECONDS:-5}
STEADY_REFRESH=${GRID_CLASSIC_STEADY_REFRESH_SECONDS:-20}

if [ ! -s "$LATEST" ]; then
  echo "No campaign yet: $LATEST" >&2
  exit 1
fi
CAMPAIGN=$(cat "$LATEST")
[ -d "$CAMPAIGN" ] || { echo "Campaign missing: $CAMPAIGN" >&2; exit 1; }
SESSION=${SESSION:-r31-grid-$(basename "$CAMPAIGN")}
printf '%s\n' "$SESSION" > "$CAMPAIGN/classic_tmux_session.txt"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux select-window -t "$SESSION:grid" 2>/dev/null || true
  exec tmux attach -t "$SESSION"
fi

for _ in $(seq 1 120); do
  [ -s "$CAMPAIGN/jobs.tsv" ] && [ "$(awk 'END{print NR}' "$CAMPAIGN/jobs.tsv")" -ge 10 ] && break
  sleep 1
done
[ -s "$CAMPAIGN/jobs.tsv" ] || { echo "jobs.tsv not available yet: $CAMPAIGN/jobs.tsv" >&2; exit 2; }

adaptive_sleep='if [ -s '"$(printf '%q' "$CAMPAIGN/ALL_NINE_RUNNING.tsv")"' ]; then sleep '"$(printf '%q' "$STEADY_REFRESH")"'; else sleep '"$(printf '%q' "$STARTUP_REFRESH")"'; fi'

for INDEX in $(seq 1 9); do
  LINE=$(awk -F '\t' -v n=$((INDEX + 1)) 'NR==n {print $0}' "$CAMPAIGN/jobs.tsv")
  TAG=$(printf '%s\n' "$LINE" | awk -F '\t' '{print $2}')
  K=$(printf '%s\n' "$LINE" | awk -F '\t' '{print $3}')
  TAU=$(printf '%s\n' "$LINE" | awk -F '\t' '{print $4}')
  LABEL="k=$K tau=$TAU"
  CMD="while true; do clear; date; echo '=== $LABEL ==='; echo 'tag: $TAG'; echo; awk -F '\\t' -v tag='$TAG' 'NR==1 || \$2==tag {print}' '$CAMPAIGN/jobs.tsv' | column -t -s \$'\\t'; echo; for marker in startup_begin.tsv startup_ok.tsv startup_failure.tsv edge_handoff.tsv VERIFIED_OK.tsv; do if [ -s '$CAMPAIGN/runs/$TAG/'\$marker ]; then echo \"MARKER \$marker: \$(head -n 1 '$CAMPAIGN/runs/$TAG/'\$marker)\"; fi; done; echo; LOG=\$(ls -1t '$CAMPAIGN/logs/$TAG'.slurm-*.log '$CAMPAIGN/logs/$TAG'.log 2>/dev/null | head -1); if [ -n \"\$LOG\" ]; then echo \"log: \$LOG\"; echo; tail -n 42 \"\$LOG\"; else echo 'Waiting for this wave/job log...'; fi; $adaptive_sleep; done"
  if [ "$INDEX" -eq 1 ]; then
    tmux new-session -d -s "$SESSION" -n grid "exec nice -n 15 bash -lc $(printf '%q' "$CMD")"
    PANE=$(tmux display-message -p -t "$SESSION:grid" '#{pane_id}')
  else
    PANE=$(tmux split-window -d -P -F '#{pane_id}' -t "$SESSION:grid" "exec nice -n 15 bash -lc $(printf '%q' "$CMD")")
  fi
  tmux select-pane -t "$PANE" -T "$LABEL"
  tmux select-layout -t "$SESSION:grid" tiled >/dev/null
done

tmux set-option -t "$SESSION" pane-border-status top
tmux set-option -t "$SESSION" pane-border-format '#[bold] #{pane_title} #[default]'
tmux set-option -t "$SESSION" remain-on-exit on

FOUNDRY="while true; do clear; date; echo '=== EDGE FOUNDRY (cached only) ==='; echo 'campaign: $CAMPAIGN'; echo; if [ -s '$CAMPAIGN/edge_foundry_job.tsv' ]; then column -t -s \$'\\t' '$CAMPAIGN/edge_foundry_job.tsv'; else echo 'Waiting for foundry job record...'; fi; echo; if [ -s '$CAMPAIGN/edge_foundry/EDGE_READY.tsv' ]; then echo 'EDGE_READY:'; cat '$CAMPAIGN/edge_foundry/EDGE_READY.tsv'; elif [ -s '$CAMPAIGN/edge_foundry/EDGE_FAILED.tsv' ]; then echo 'LATEST FAILURE:'; cat '$CAMPAIGN/edge_foundry/EDGE_FAILED.tsv'; else echo 'Not ready yet.'; fi; echo; if [ -s '$CAMPAIGN/edge_foundry/edge_foundry.log' ]; then tail -n 72 '$CAMPAIGN/edge_foundry/edge_foundry.log'; fi; $adaptive_sleep; done"
tmux new-window -t "$SESSION" -n foundry "exec nice -n 15 bash -lc $(printf '%q' "$FOUNDRY")"

CONTROL="while true; do clear; date; echo '=== SUPERVISOR (FILE CACHE ONLY) ==='; echo 'NO squeue/scontrol from this viewer'; echo; tail -n 38 '$CAMPAIGN/logs/supervisor.log' 2>/dev/null; echo; echo '=== EDGE FOUNDRY ==='; if [ -s '$CAMPAIGN/edge_foundry_job.tsv' ]; then column -t -s \$'\t' '$CAMPAIGN/edge_foundry_job.tsv'; else echo 'Waiting for foundry record...'; fi; echo; echo '=== CACHED JOB MANIFEST ==='; if [ -s '$CAMPAIGN/jobs.tsv' ]; then column -t -s \$'\t' '$CAMPAIGN/jobs.tsv'; fi; echo; echo '=== CACHED CLUSTER VIEW ==='; if [ -s '$CAMPAIGN/cluster_live.tsv' ]; then stat -c 'cache updated: %y' '$CAMPAIGN/cluster_live.tsv' 2>/dev/null || true; head -n 28 '$CAMPAIGN/cluster_live.tsv' | column -t -s \$'\t'; fi; $adaptive_sleep; done"
tmux new-window -t "$SESSION" -n control "exec nice -n 15 bash -lc $(printf '%q' "$CONTROL")"

tmux select-window -t "$SESSION:grid"
exec tmux attach -t "$SESSION"
