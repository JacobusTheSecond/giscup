#!/usr/bin/env python3
from pathlib import Path
root=Path(__file__).resolve().parents[1]
s=(root/'src/main.cpp').read_text()
worker=(root/'run_v31_vertex_cell.sbatch').read_text()
start24=(root/'start_v31_24h.sh').read_text()
start6=(root/'start_v34_6h_data.sh').read_text()
required=[
    'double sim_anneal_epoch_seconds = 105.0;',
    'double sim_anneal_growth_min_seconds = 90.0;',
    'double sim_anneal_growth_max_seconds = 300.0;',
    'double portfolio_sa_max_seconds = 900.0;',
    '300.0 + 200.0 * static_cast<double>(exploration_level)',
    '300.0 + 600.0 * std::pow(post_k_progress, 0.80)',
    'soft-deadline+adaptive-grace, partial-salvage',
    'repair_seconds_per_evaluated_candidate',
    'repair_partial_swaps_salvaged',
    'repair_admission_skips',
    'if (partial_scan) ++result.repair_partial_swaps_salvaged;',
    'if (partial_scan) break;',
    'predicted_scan_seconds > available_seconds',
    '0.20 * time_limit',
]
for needle in required:
    if needle not in s: raise SystemExit(f'missing v34 invariant: {needle}')
block=s[s.find('// RINGARC34: periodic live-state cached 1-swap repair.'):
        s.find('if (current_quality > result.best_quality', s.find('// RINGARC34: periodic live-state cached 1-swap repair.'))]
if 'if (repair_search.deadline_exhausted)' in block:
    raise SystemExit('old throw-away-on-timeout repair guard survived')
if 'better_search_quality(' not in block or 'evaluate_arc_replacement_quality(' not in block:
    raise SystemExit('partial salvage lost authoritative quality validation')
for needle in [
    'GRID_SA_EPOCH_SECONDS=${GRID_SA_EPOCH_SECONDS:-105}',
    'GRID_SA_GROWTH_MIN_SECONDS=${GRID_SA_GROWTH_MIN_SECONDS:-90}',
    'GRID_SA_GROWTH_MAX_SECONDS=${GRID_SA_GROWTH_MAX_SECONDS:-300}',
    'GRID_SA_REPAIR_GRACE_SECONDS=${GRID_SA_REPAIR_GRACE_SECONDS:-30}',
    'GRID_SA_REPAIR_MAX_GRACE_SECONDS=${GRID_SA_REPAIR_MAX_GRACE_SECONDS:-180}',
    'GRID_PORTFOLIO_SA_MAX_SECONDS=${GRID_PORTFOLIO_SA_MAX_SECONDS:-900}',
]:
    if needle not in worker and needle not in start24:
        raise SystemExit(f'missing launcher/worker knob: {needle}')
for needle in [
    'INPUT=${INPUT:-$ROOT/data/data.geojson}',
    'GRID_HORIZON=${GRID_HORIZON:-5.5h}',
    'GRID_SLURM_TIME=${GRID_SLURM_TIME:-06:00:00}',
    'GRID_INPUT_PREFLIGHT_MODE=${GRID_INPUT_PREFLIGHT_MODE:-strict}',
]:
    if needle not in start6: raise SystemExit(f'missing 6h verifier launcher invariant: {needle}')
print('RINGARC34 dynamic SA/adaptive repair invariants: PASS')
