#!/usr/bin/env python3
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from summary_run_impl import parse_log
text='''
Simulated annealing phase [greedy_sim_anneal] cardinality=500: time=5.0 min, pool=all
Portfolio cycle 7: budget=20.0 min, exact-1-swap=4.0 min, SA=11.7 min, ruin=3.0 min, crossover=1.3 min (exploration_level=2), elites=8, pending_excursions=0, stagnation=6, score=1/2, near_progress=0.1.
Simulated annealing [x] group 1/16 incremental summary: pair_cache_hits=0, pair_cache_misses=0, context_rebuilds=2, full_union_rebuilds=1, downhill_attempted=20, downhill_accepted=4, primary_downhill=1/2, max_primary_drawdown=1, primary_escape_wins=1, repair=2/4 triggers=1 full_scans=3 eval=1000 cache_recompute=200 cache_rebuilds=1 cache_refreshes=2 cached_pruned=5000 repair_blocks=7/8 partial_scans=1 partial_salvaged=1 admission_skips=2 repair_time=12.5 max_scan=7.5 predicted_scan=9.0 max_grace=30, downhill_acceptance=20.00%, initial_calibrated_T=1.2, current_T=0.2, reheats=1, temperature_role=warm, best_score=10, current_score=9, total_polygons=100.
Ruin/recreate attempt 1/1 [uniform-random, stagnation_level=1]: destroyed=24/54, focus_polygons=5, post_ruin_pool=100, returned_removed=0/24, score=11/100, improves_incumbent=yes, near_progress=0.2, covered=3 m, best=11, remaining=1.0 min.
'''
with tempfile.TemporaryDirectory() as td:
    p=Path(td)/'x.log'; p.write_text(text)
    x=parse_log(p)
assert x['sa_phase_seconds']==[300.0], x
assert x['post_k_sa_assigned_seconds']==[702.0], x
assert x['repair_full_scans']==3 and x['repair_partial_scans']==1, x
assert x['repair_partial_salvaged']==1 and x['repair_admission_skips']==2, x
assert abs(x['repair_scan_seconds']-12.5)<1e-9 and abs(x['repair_max_predicted_scan_seconds']-9.0)<1e-9, x
assert x['ruin_modes']['uniform-random']['incumbent_improvements']==1, x
print('RINGARC34 report telemetry parser: PASS')
