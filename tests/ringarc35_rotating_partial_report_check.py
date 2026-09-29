#!/usr/bin/env python3
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.summary_run_impl import parse_log  # noqa: E402

line = '''Simulated annealing [post_k_portfolio_annealer] group 1/16 incremental summary: pair_cache_hits=0, pair_cache_misses=0, context_rebuilds=1, full_union_rebuilds=1, downhill_attempted=1, downhill_accepted=1, primary_downhill=1/1, max_primary_drawdown=1, primary_escape_wins=0, repair=1/3 triggers=2 full_scans=1 eval=12345 cache_recompute=22 cache_rebuilds=1 cache_refreshes=2 cached_pruned=345 repair_blocks=19/4 partial_scans=2 partial_salvaged=1 admission_skips=0 repair_time=200 max_scan=110 predicted_scan=95 max_grace=30 rotating_partial=2 rotating_blocks=11 rotating_elite=4 rotating_cursor=17->18, downhill_acceptance=10.00%, initial_calibrated_T=1, current_T=0.5, reheats=0, temperature_role=warm, best_score=10, current_score=10, total_polygons=100.\n'''
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'run.log'
    p.write_text(line)
    parsed = parse_log(p)

assert parsed['repair_partial_scans'] == 2
assert parsed['repair_partial_salvaged'] == 1
assert parsed['repair_rotating_partial_scans'] == 2
assert parsed['repair_rotating_blocks_considered'] == 11
assert parsed['repair_rotating_elite_prefix_blocks'] == 4
assert parsed['repair_rotating_last_start_rank'] == 17
assert parsed['repair_rotating_next_rank'] == 18
print('RINGARC35.0.3 rotating partial-repair report parser: PASS')
