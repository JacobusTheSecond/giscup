#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
INPUT=${INPUT:-$ROOT/data/la_large.geojson}
GRID_NAME=${GRID_NAME:-r31_fullrun_freeze_9plus1}
EXPECTED_REVISION=RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE
fail(){ echo "ERROR: $*" >&2; exit 1; }
command -v sbatch >/dev/null 2>&1 || fail "Slurm sbatch unavailable"
command -v squeue >/dev/null 2>&1 || fail "Slurm squeue unavailable"
command -v tmux >/dev/null 2>&1 || fail "tmux unavailable"
command -v nice >/dev/null 2>&1 || fail "nice unavailable"
[ -x "$ENV/bin/python" ] || fail "missing $ENV/bin/python"
[ -x "$ROOT/build/gis_cup_visibility" ] || fail "build missing; run ./submit_build_v31.sh"
[ -s "$ROOT/build/gis_cup_visibility.revision" ] || fail "build revision manifest missing"
read -r REV SHA _ < "$ROOT/build/gis_cup_visibility.revision"
[ "$REV" = "$EXPECTED_REVISION" ] || fail "expected $EXPECTED_REVISION, found $REV"
ACTUAL=$(sha256sum "$ROOT/build/gis_cup_visibility" | awk '{print $1}')
[ "$ACTUAL" = "$SHA" ] || fail "build checksum mismatch"
INPUT=$(realpath -e "$INPUT") || fail "input missing: $INPUT"
[ -s "$INPUT" ] || fail "input empty: $INPUT"

# Competition campaign owns the complete personal Slurm allowance: nine worker
# jobs plus one edge foundry.  Refuse to start if anything else is already in
# the queue so the supervisor never competes with unrelated jobs for the cap.
if [ "${GRID_REQUIRE_EMPTY_USER_QUEUE:-1}" = 1 ]; then
  if ! EXISTING=$(squeue -h -u "$USER" 2>&1); then
    fail "could not query your Slurm queue; refusing to launch: $EXISTING"
  fi
  if [ -n "$EXISTING" ]; then
    echo "Your Slurm queue is not empty; refusing to start the 9+1 campaign." >&2
    squeue -u "$USER" -o '%.18i %.34j %.2t %.10M %.10l %.5C %.12m %.36R' >&2
    exit 1
  fi
fi

CAMPAIGN_ID=${CAMPAIGN_ID:-$(date +%Y%m%d_%H%M%S)}
CAMPAIGN_DIR=${CAMPAIGN_DIR:-$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME/$CAMPAIGN_ID}
mkdir -p "$CAMPAIGN_DIR/logs" "$CAMPAIGN_DIR/runs"
LAUNCH_ENV="$CAMPAIGN_DIR/launch_environment.sh"
TMUX_SESSION=${TMUX_SESSION:-r31-${GRID_NAME}-${CAMPAIGN_ID}}

# Freeze-oriented 24h allocation: approximately 23h actual optimizer horizon,
# no heavy competition-day verifier reserve, late authenticated edge search at
# ~30%, and a bounded selected-edge threshold wiggle tail.
export ROOT ENV INPUT GRID_NAME CAMPAIGN_ID CAMPAIGN_DIR LAUNCH_ENV TMUX_SESSION
export GRID_K_VALUES=${GRID_K_VALUES:-500,5000,10000}
export GRID_TAU_VALUES=${GRID_TAU_VALUES:-0.25,0.50,0.75}
export GRID_HORIZON=${GRID_HORIZON:-23h}
export GRID_SLURM_TIME=${GRID_SLURM_TIME:-1-00:00:00}
export GRID_VERIFY=${GRID_VERIFY:-0}
export GRID_VERIFY_RESERVE_SECONDS=0
export GRID_SA_EPOCH_SECONDS=${GRID_SA_EPOCH_SECONDS:-105}
export GRID_SA_GROWTH_MIN_SECONDS=${GRID_SA_GROWTH_MIN_SECONDS:-90}
export GRID_SA_GROWTH_MAX_SECONDS=${GRID_SA_GROWTH_MAX_SECONDS:-300}
export GRID_SA_REPAIR_GRACE_SECONDS=${GRID_SA_REPAIR_GRACE_SECONDS:-30}
export GRID_SA_REPAIR_MAX_GRACE_SECONDS=${GRID_SA_REPAIR_MAX_GRACE_SECONDS:-180}
export GRID_SA_REPAIR_ADMISSION_FACTOR=${GRID_SA_REPAIR_ADMISSION_FACTOR:-1.15}
export GRID_PORTFOLIO_SA_MAX_SECONDS=${GRID_PORTFOLIO_SA_MAX_SECONDS:-900}
export GRID_EDGE_ENABLE=${GRID_EDGE_ENABLE:-1}
export GRID_EDGE_SEARCH_FRACTION=${GRID_EDGE_SEARCH_FRACTION:-0.30}
export GRID_EDGE_SA_PROPOSAL_FRACTION=${GRID_EDGE_SA_PROPOSAL_FRACTION:-0.125}
export GRID_EDGE_SA_MIN_PROPOSAL_BATCH=${GRID_EDGE_SA_MIN_PROPOSAL_BATCH:-100000}
export GRID_EDGE_SA_MAX_PROPOSAL_BATCH=${GRID_EDGE_SA_MAX_PROPOSAL_BATCH:-500000}
export GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS=${GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS:-2}
export GRID_EDGE_SAMPLED_REMOVAL_GROUPS=${GRID_EDGE_SAMPLED_REMOVAL_GROUPS:-4}
export GRID_SAMPLED_REMOVAL_SLOTS=${GRID_SAMPLED_REMOVAL_SLOTS:-8}
export GRID_SAMPLED_REMOVAL_ANCHORS=${GRID_SAMPLED_REMOVAL_ANCHORS:-2}
export GRID_EDGE_RECHECK_SECONDS=${GRID_EDGE_RECHECK_SECONDS:-600}
export GRID_WIGGLE_ENABLE=${GRID_WIGGLE_ENABLE:-1}
export GRID_WIGGLE_SECONDS=${GRID_WIGGLE_SECONDS:-1800}
export GRID_WIGGLE_STEP_METERS=${GRID_WIGGLE_STEP_METERS:-0.001}
# Claim-only slack.  Search/acceptance remain strict.  This is intentionally
# isolated so tomorrow's website report can calibrate a single number without
# touching the optimizer or guard set.
export GRID_SUBMISSION_CLAIM_SLACK_METERS=${GRID_SUBMISSION_CLAIM_SLACK_METERS:-1e-8}
# The gateway supervisor is deliberately sleepy: one batched scheduler poll per
# minute in steady state.  The rich dashboard refreshes every 10s from cached
# files and does not query Slurm itself.
export GRID_MONITOR_POLL_SECONDS=${GRID_MONITOR_POLL_SECONDS:-60}
export GRID_FOUNDRY_MONITOR_POLL_SECONDS=${GRID_FOUNDRY_MONITOR_POLL_SECONDS:-30}
export GRID_ADMISSION_POLL_SECONDS=${GRID_ADMISSION_POLL_SECONDS:-3}
export GRID_CLUSTER_STARTUP_REFRESH_SECONDS=${GRID_CLUSTER_STARTUP_REFRESH_SECONDS:-5}
export GRID_WORKER_MEM_GB=${GRID_WORKER_MEM_GB:-128}
export EDGE_FOUNDRY_CPUS=${EDGE_FOUNDRY_CPUS:-48}
export EDGE_FOUNDRY_MEM_GB=${EDGE_FOUNDRY_MEM_GB:-512}
export GRID_STARTUP_POLL_SECONDS=${GRID_STARTUP_POLL_SECONDS:-3}
export GRID_RETRY_COOLDOWN_SECONDS=${GRID_RETRY_COOLDOWN_SECONDS:-2}
export GRID_JOB_SLOT_POLL_SECONDS=${GRID_JOB_SLOT_POLL_SECONDS:-3}
export GRID_STEADY_NICE_DELTA=${GRID_STEADY_NICE_DELTA:-10}
export GRID_DASHBOARD_REFRESH_SECONDS=${GRID_DASHBOARD_REFRESH_SECONDS:-10}
export GRID_USER_JOB_LIMIT=${GRID_USER_JOB_LIMIT:-10}
export GRID_INPUT_PREFLIGHT_MODE=${GRID_INPUT_PREFLIGHT_MODE:-off}
export BUILDING_ID_MODE=${BUILDING_ID_MODE:-source}
export BUILDING_ID_FIELD=${BUILDING_ID_FIELD:-id}
export DATA_ROOT=${DATA_ROOT:-$ROOT}
# The outer launcher already performed the empty-queue gate.  The supervisor
# itself is a gateway tmux process, not a Slurm job.
export GRID_REQUIRE_EMPTY_USER_QUEUE=0

"$ENV/bin/python" - "$LAUNCH_ENV" <<'PY'
from pathlib import Path
import os, shlex, sys
p=Path(sys.argv[1])
keys={"ROOT","ENV","INPUT","DATA_ROOT","GRID_NAME","CAMPAIGN_ID","CAMPAIGN_DIR","LAUNCH_ENV","TMUX_SESSION","BUILDING_ID_MODE","BUILDING_ID_FIELD"}
keys.update(k for k in os.environ if k.startswith('GRID_') or k.startswith('EDGE_'))
lines=['# generated RINGARC35 launch environment']
for k in sorted(keys):
    if k in os.environ: lines.append(f'export {k}={shlex.quote(os.environ[k])}')
p.write_text('\n'.join(lines)+'\n')
PY
chmod 600 "$LAUNCH_ENV"

LATEST_ROOT="$ROOT/output/portfolio_annealer_edgepool_exact_gate_9plus1/$GRID_NAME"
mkdir -p "$LATEST_ROOT"
printf '%s\n' "$CAMPAIGN_DIR" > "$LATEST_ROOT/latest_campaign"
printf '%s\n' "$TMUX_SESSION" > "$CAMPAIGN_DIR/tmux_session.txt"

# Optional compatibility preflight is deliberately OFF by default for this
# known full-run dataset: loading a giant GeoJSON on the weak gateway is not
# worth it.  It can be run manually later if desired.
if [ "$GRID_INPUT_PREFLIGHT_MODE" != off ]; then
  "$ENV/bin/python" "$ROOT/tools/preflight_competition_input.py" \
    "$INPUT" --mode "$GRID_INPUT_PREFLIGHT_MODE" --id-field "$BUILDING_ID_FIELD" \
    --report "$CAMPAIGN_DIR/input_preflight.txt" || {
      [ "$GRID_INPUT_PREFLIGHT_MODE" = strict ] && exit 2
    }
fi

if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  fail "tmux session already exists: $TMUX_SESSION"
fi

# Two tiny gateway processes only: one mostly-sleeping supervisor and one
# file-only dashboard renderer.  Neither does solver work.  The only Slurm jobs
# created are the nine workers and one edge foundry.
SUP_CMD="source $(printf '%q' "$LAUNCH_ENV"); exec $(printf '%q' "$ENV/bin/python") -u $(printf '%q' "$ROOT/submit_v31_supervisor.py") >> $(printf '%q' "$CAMPAIGN_DIR/logs/supervisor.log") 2>&1"
DASH_CMD="source $(printf '%q' "$LAUNCH_ENV"); sleep 3; exec nice -n 15 $(printf '%q' "$ENV/bin/python") $(printf '%q' "$ROOT/tools/render_v31_dashboard.py") $(printf '%q' "$CAMPAIGN_DIR") --watch $(printf '%q' "$GRID_DASHBOARD_REFRESH_SECONDS")"

tmux new-session -d -s "$TMUX_SESSION" -n supervisor "bash -lc $(printf '%q' "$SUP_CMD")"
tmux new-window -t "$TMUX_SESSION:" -n dashboard "bash -lc $(printf '%q' "$DASH_CMD")"
tmux select-window -t "$TMUX_SESSION:dashboard"

cat <<EOF2
RINGARC36.1 sampled-removal hybrid campaign started.
Campaign:    $CAMPAIGN_DIR
Input:       $INPUT
Slurm cap:   exactly 9 workers + 1 edge foundry; supervisor/dashboard stay on gateway
Search:      $GRID_HORIZON inside a 24h worker allocation
Late edge:   ${GRID_EDGE_SEARCH_FRACTION} of discrete budget; SA screen ${GRID_EDGE_SA_PROPOSAL_FRACTION} (${GRID_EDGE_SA_MIN_PROPOSAL_BATCH}-${GRID_EDGE_SA_MAX_PROPOSAL_BATCH}), live repair OFF
SA explore:  sampled-removal hot groups vertex=${GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS}, edge=${GRID_EDGE_SAMPLED_REMOVAL_GROUPS}; slots=${GRID_SAMPLED_REMOVAL_SLOTS} (${GRID_SAMPLED_REMOVAL_ANCHORS} low-loss anchors)
Wiggle:      up to ${GRID_WIGGLE_SECONDS}s
Claim slack: ${GRID_SUBMISSION_CLAIM_SLACK_METERS} m (claimed-ID line only; strict optimizer unchanged)
Dashboard:   ${GRID_DASHBOARD_REFRESH_SECONDS}s refresh, cached files only, no squeue calls
Supervisor:  wave startup, ${GRID_WORKER_MEM_GB}GiB/worker with CPU-first placement; ${GRID_CLUSTER_STARTUP_REFRESH_SECONDS}s cluster cache during admission
Foundry:     ${EDGE_FOUNDRY_CPUS} CPUs / ${EDGE_FOUNDRY_MEM_GB} GiB RAM; steady state supervisor poll ~every ${GRID_MONITOR_POLL_SECONDS}s
Tmux:        $TMUX_SESSION

Attach rich dashboard:
  tmux attach -t '$TMUX_SESSION'

Classic 3-window viewer (9-cell grid / foundry / control; file-only):
  ./watch_v31_classic_tmux.sh

Or single rich dashboard:
  ./watch_v31_dashboard.sh

One-shot status:
  ./snapshot_v31_dashboard.sh

Cancel campaign and tmux:
  ./cancel_v31_campaign.sh
EOF2
