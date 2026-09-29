#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
cd "$ROOT"

echo "[v31] shell syntax"
for f in \
  build_v31.sh submit_build_v31.sh start_v31_24h.sh start_v34_6h_data.sh \
  run_v31_vertex_cell.sbatch run_v31_edge_foundry.sbatch \
  watch_v31_dashboard.sh watch_v31_classic_tmux.sh snapshot_v31_dashboard.sh cancel_v31_campaign.sh \
  summarize_v31_campaign.sh zip_campaign_solutions.sh; do
  bash -n "$f"
done

echo "[v31] python syntax"
python3 -m py_compile \
  submit_v31_supervisor.py summarize_ringarc31_run.py summarize_ringarc31_campaign.py \
  make_competition_submission.py \
  tools/campaign_utils.py tools/collect_campaign_solutions.py \
  tools/preflight_competition_input.py tools/render_v31_dashboard.py \
  tools/submission_format.py tools/summary_run_impl.py tools/summary_campaign_impl.py

echo "[v31] release/source checks"
python3 tests/ringarc31_release_source_check.py
python3 tests/ringarc31_parameter_generalization_check.py
python3 tests/ringarc31_resource_allocator_check.py
python3 tests/ringarc31_preflight_check.py
python3 tests/ringarc31_campaign_recovery_check.py
python3 tests/ringarc31_dashboard_check.py

echo "[v31] temperature-softmax SA policy"
grep -Fq 'GRID_SA_PROPOSAL_BATCH=${GRID_SA_PROPOSAL_BATCH:-1024}' run_v31_vertex_cell.sbatch
grep -Fq -- '--sim-anneal-min-step-pool "$GRID_SA_PROPOSAL_BATCH"' run_v31_vertex_cell.sbatch
grep -Fq -- '--sim-anneal-max-step-pool "$GRID_SA_PROPOSAL_BATCH"' run_v31_vertex_cell.sbatch
python3 tests/ringarc31_sa_temperature_selector_check.py
python3 tests/ringarc31_sa_live_repair_check.py
python3 tests/ringarc34_dynamic_sa_repair_check.py
python3 tests/ringarc34_report_telemetry_check.py

bash -n watch_v31_classic_tmux.sh
if grep -Fq '\\$2' watch_v31_classic_tmux.sh; then
  echo "ERROR: classic viewer contains double-escaped positional parameter (set -u regression)" >&2
  exit 1
fi
if grep -Eq 'squeue[[:space:]]+-u|scontrol[[:space:]]+show|sacct[[:space:]]+-' watch_v31_classic_tmux.sh; then
  echo "ERROR: classic tmux viewer must remain scheduler-blind" >&2
  exit 1
fi
python3 tests/cpp_delimiter_balance_check.py
python3 tests/cpp_consecutive_duplicate_declaration_check.py
python3 tests/candidate_append_arity_source_check.py
python3 tests/timeline_record_signature_check.py

echo "[v31] STL RNG compile regression"
if grep -Fq 'unit(rng())' src/main.cpp; then
  echo "ERROR: std::uniform_real_distribution must receive the RNG engine, not rng() output" >&2
  exit 1
fi
grep -Fq 'unit(rng),' src/main.cpp
cat > /tmp/r31_rng_distribution_compile_check.cpp <<'CPP'
#include <algorithm>
#include <limits>
#include <random>
int main() {
  std::mt19937_64 rng(1);
  std::uniform_real_distribution<double> unit(0.0, 1.0);
  const double u = std::clamp(unit(rng), std::numeric_limits<double>::min(),
                              1.0 - std::numeric_limits<double>::epsilon());
  return (u > 0.0 && u < 1.0) ? 0 : 1;
}
CPP
${CXX:-c++} -std=c++20 -fsyntax-only /tmp/r31_rng_distribution_compile_check.cpp
rm -f /tmp/r31_rng_distribution_compile_check.cpp

echo "[v31] exact/codec C++ harnesses"
python3 tests/ringarc29_candidate_compaction_cpp_harness.py
python3 tests/ringarc29_sa_seed_scaling_cpp_harness.py
python3 tests/arc_swap_cpp_harness.py
python3 tests/canonical_candidate_identity_cpp_harness.py
python3 tests/search_quality_cpp_harness.py
python3 tests/stepped_threshold_cache_cpp_harness.py
python3 tests/ring_arc_cache_codec_check.py

echo "[v31] packaging"
python3 tests/competition_submission_packager_check.py

# The cluster-side official evaluator experiment is deliberately absent from
# this freeze release; website verification is post-hoc and must not consume a
# competition Slurm slot.
for f in submit_official_verifier.sh verify_with_official_evaluator.sh run_v31_supervisor.sbatch; do
  if [ -e "$f" ]; then
    echo "ERROR: freeze release unexpectedly contains $f" >&2
    exit 1
  fi
done

echo "RINGARC31_VALIDATION_PASS"
