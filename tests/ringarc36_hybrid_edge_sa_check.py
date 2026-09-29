#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()
WORKER = (ROOT / 'run_v31_vertex_cell.sbatch').read_text()
START = (ROOT / 'start_v31_24h.sh').read_text()
SUP = (ROOT / 'submit_v31_supervisor.py').read_text()
REV = 'RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE'
assert (ROOT / 'VERSION').read_text().strip() == REV
assert f'PROGRAM_REVISION = "{REV}"' in SRC

# Vertex defaults remain the v35 values; the broad screen is launcher-scoped to
# authenticated visibility mode only.
assert 'std::size_t sim_anneal_min_step_pool_size = 1024;' in SRC
assert 'std::size_t sim_anneal_max_step_pool_size = 1024;' in SRC
for text in (WORKER, START, SUP):
    assert 'GRID_EDGE_SA_PROPOSAL_FRACTION' in text
    assert '0.125' in text
    assert 'GRID_EDGE_SA_MIN_PROPOSAL_BATCH' in text
    assert '100000' in text
    assert 'GRID_EDGE_SA_MAX_PROPOSAL_BATCH' in text
    assert '500000' in text

edge_args = WORKER[WORKER.index('EDGE_HYBRID_ARGS=('):]
edge_args = edge_args[:edge_args.index('\n)') + 2]
for needle in [
    '--sim-anneal-step-pool-fraction "$GRID_EDGE_SA_PROPOSAL_FRACTION"',
    '--sim-anneal-min-step-pool "$GRID_EDGE_SA_MIN_PROPOSAL_BATCH"',
    '--sim-anneal-max-step-pool "$GRID_EDGE_SA_MAX_PROPOSAL_BATCH"',
    '--no-sim-anneal-live-repair',
]:
    assert needle in edge_args, needle

# The edge args are only attached after authenticated foundry acceptance. The
# default vertex-resume path starts with an empty override array.
assert 'SECOND_MODE="vertex"' in WORKER
assert 'SECOND_OPTIMIZER_ARGS=()' in WORKER
vis = WORKER.index('if edge_foundry_authenticated; then')
assert 'SECOND_MODE="visibility"' in WORKER[vis:vis+1200]
assert 'SECOND_OPTIMIZER_ARGS=("${EDGE_HYBRID_ARGS[@]}")' in WORKER[vis:vis+1200]

# Live repair is gated cleanly at both cache construction and trigger sites.
assert 'bool sim_anneal_live_repair = true;' in SRC
assert 'if (use_stepped_add_cache && options.sim_anneal_live_repair)' in SRC
assert 'if (options.sim_anneal_live_repair &&\n                    accepted_since_repair' in SRC

# Broad exact screening keeps v35 thermal selection bounded to 1024 finalists.
for needle in [
    'constexpr std::size_t selector_shortlist_limit = 1024U;',
    'std::nth_element(',
    'proposals.resize(selector_shortlist_limit);',
    'selector=temperature-softmax-all(4xT)',
]:
    assert needle in SRC, needle

# RINGARC36.1 explorer plumbing: construction is protected by the post_k_ gate;
# vertex uses 2 hot groups and edge overrides to 4.
for text in (WORKER, START, SUP):
    assert 'GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS' in text
    assert 'GRID_EDGE_SAMPLED_REMOVAL_GROUPS' in text
    assert 'GRID_SAMPLED_REMOVAL_SLOTS' in text
    assert 'GRID_SAMPLED_REMOVAL_ANCHORS' in text
assert '--sim-anneal-sampled-removal-groups "$GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS"' in WORKER
assert '--sim-anneal-sampled-removal-groups "$GRID_EDGE_SAMPLED_REMOVAL_GROUPS"' in edge_args
for needle in [
    'phase_label.rfind("post_k_", 0) == 0',
    'group_id >= group_count - sampled_removal_group_count',
    'sampled_removal_explorer',
    'removal_mode=',
]:
    assert needle in SRC, needle

print('RINGARC36.1 hybrid edge-SA + sampled-removal explorer invariants: PASS')
