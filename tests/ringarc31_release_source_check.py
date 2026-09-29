#!/usr/bin/env python3
from pathlib import Path
import importlib.util, os, struct, sys
ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/'src/main.cpp').read_text()
WORKER=(ROOT/'run_v31_vertex_cell.sbatch').read_text()
SUP=(ROOT/'submit_v31_supervisor.py').read_text()
START=(ROOT/'start_v31_24h.sh').read_text()
WATCH=(ROOT/'watch_v31_dashboard.sh').read_text()
DASH=(ROOT/'tools/render_v31_dashboard.py').read_text()
REV=(ROOT/'VERSION').read_text().strip()
EXPECTED='RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE'
assert REV == EXPECTED
assert f'PROGRAM_REVISION = "{EXPECTED}"' in SRC

# Preserve the v30.0.2.1 explore -> harvest changes.
for needle in [
    'std::size_t sim_anneal_min_step_pool_size = 1024;',
    'std::size_t sim_anneal_max_step_pool_size = 1024;',
    'mode=stochastic-insertion/exact-best-removal',
    'selector=temperature-softmax-all(4xT)',
    'constexpr double selector_temperature_multiplier = 4.0;',
    'for (const CachedPairProposal& proposal : proposals)',
    'selector_delta / selector_temperature + gumbel',
    'temperature_hot_fraction',

    'result.accepted_primary_downhill',
    'result.max_primary_drawdown',
    'result.primary_escape_wins',
    'Destruction must happen before we decide which candidates are',
    'post_ruin_pool=',
    'const std::size_t tabu_fill_target =',
    'mode_name = "frontier-building";',
    'mode_name = "coverage-neighborhood";',
    'constexpr std::size_t ruin_mode_count = 9U;',
    'mode_name = "uniform-random";',
    'mode_name = "building-star";',
    'mode_name = "stochastic-low-loss";',
    'mode_name = "dispersed-random";',
    'mode_name = "visibility-graph-cluster";',
    'bool one_swap_known_optimal = false;',
]: assert needle in SRC, needle

# Claim slack is output-only: writer can include tiny negative margins, but the
# optimizer qualification primitive remains strict.
for needle in [
    'double submission_claim_slack_meters = 0.001;',
    '--submission-claim-slack-meters',
    'const bool slack_qualified = !exactly_qualified &&',
    'margin >= -options.submission_claim_slack_meters;',
    'claim-only slack=',
    'extras=',
    'worst_extra_margin=',
    'submission_claim_slack.tsv',
    'ClaimSlackRecord{building_id, margin, visible, target}',
]: assert needle in SRC, needle
assert '--submission-claim-slack-meters "$GRID_SUBMISSION_CLAIM_SLACK_METERS"' in WORKER
assert 'GRID_SUBMISSION_CLAIM_SLACK_METERS=${GRID_SUBMISSION_CLAIM_SLACK_METERS:-1e-8}' in WORKER

# Full dress rehearsal: 23h optimizer inside a 24h worker allocation, no heavy
# verifier reserve, 30% late edge pool, bounded 20m wiggle.
for needle in [
    'GRID_HORIZON=${GRID_HORIZON:-23h}',
    'GRID_SLURM_TIME=${GRID_SLURM_TIME:-1-00:00:00}',
    'GRID_VERIFY=${GRID_VERIFY:-0}',
    'GRID_EDGE_SEARCH_FRACTION=${GRID_EDGE_SEARCH_FRACTION:-0.30}',
    'GRID_WIGGLE_SECONDS=${GRID_WIGGLE_SECONDS:-1800}',
    'GRID_SUBMISSION_CLAIM_SLACK_METERS=${GRID_SUBMISSION_CLAIM_SLACK_METERS:-1e-8}',
]: assert needle in START, needle

# Hard 10-job ceiling: the supervisor is a low-load gateway tmux process, never
# a Slurm control job.  It guards every sbatch against the per-user cap.
assert 'run_v31_supervisor.sbatch' not in START
assert 'r31-control' not in START
assert 'tmux new-session' in START and 'submit_v31_supervisor.py' in START
assert 'nice -n 10' not in START and 'nice -n 15' in START
assert 'GRID_USER_JOB_LIMIT=${GRID_USER_JOB_LIMIT:-10}' in START
assert 'def wait_for_user_job_slot' in SUP
assert 'if envelope.attempt > 0:' in SUP
assert 'wait_for_user_job_slot("retry cell {} attempt {}".format(task.tag, envelope.attempt))' in SUP
assert 'if attempt > 1:' in SUP
assert 'wait_for_user_job_slot("edge foundry retry attempt {}".format(attempt))' in SUP
assert 'enter_steady_state_niceness()' in SUP
assert 'submit_summary(' not in SUP
assert 'No automatic summary Slurm job is submitted' in SUP

# Rich dashboard: 10-second visual refresh, but it is scheduler-blind.  Only
# cached files/log tails are read, avoiding a second stream of Slurm RPCs.
assert 'GRID_DASHBOARD_REFRESH_SECONDS=${GRID_DASHBOARD_REFRESH_SECONDS:-10}' in START
assert 'REFRESH=${GRID_DASHBOARD_REFRESH_SECONDS:-10}' in WATCH
assert 'seconds = max(10, args.watch)' in DASH
assert 'import subprocess' not in DASH
assert 'subprocess.run' not in DASH
assert 'FILE-ONLY renderer' in DASH
assert 'GRID_MONITOR_POLL_SECONDS=${GRID_MONITOR_POLL_SECONDS:-60}' in START
assert 'GRID_ADMISSION_POLL_SECONDS=${GRID_ADMISSION_POLL_SECONDS:-3}' in START
assert 'GRID_STARTUP_POLL_SECONDS=${GRID_STARTUP_POLL_SECONDS:-3}' in START
assert 'GRID_RETRY_COOLDOWN_SECONDS=${GRID_RETRY_COOLDOWN_SECONDS:-2}' in START
assert 'GRID_JOB_SLOT_POLL_SECONDS=${GRID_JOB_SLOT_POLL_SECONDS:-3}' in START
assert 'GRID_STEADY_NICE_DELTA=${GRID_STEADY_NICE_DELTA:-10}' in START

# Edge late handoff remains rechecked rather than missed at a single instant.
for needle in [
    'GRID_EDGE_RECHECK_SECONDS=${GRID_EDGE_RECHECK_SECONDS:-600}',
    'scheduled late search; edge foundry will be rechecked every',
    'SLICE_SECONDS=$GRID_EDGE_RECHECK_SECONDS',
]: assert needle in WORKER, needle

# Arbitrary tau tags remain collision-proof binary64.
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('r31sup', ROOT/'submit_v31_supervisor.py')
assert spec and spec.loader
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
for tau in [0.0,0.25,0.5,0.75,0.5000000000000001,1.0]:
    assert mod.tau_token(tau)==struct.pack('>d',tau).hex()
assert mod.tau_token(0.5)!=mod.tau_token(0.5000000000000001)

print('RINGARC36 release source invariants: PASS')
