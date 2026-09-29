#!/usr/bin/env python3
from pathlib import Path
import tempfile
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.summary_run_impl import parse_log

text = '''
[wiggle][init] discrete reconstruction 1000/1000 selected-guard sample-arc rows in 12.0 s; movable_edge_guards=317, frozen_vertices=683, cached_score=6117, reconstructed_score=6117, qualification_set_match=yes, max_coverage_delta=1e-09 m.
[wiggle][round 1] begin: movable_edge_guards=317, frozen_vertices=683, near_threshold_targets=44, step=0.001 m, batch=256, scoring=discrete-sample-arcs(score->near-progress; total tie-break).
[wiggle] complete: rounds=4, movable_edge_guards=317, frozen_vertices=683, trials=2450, completed_rows=2450, accepted=19, momentum_continuations=7, score_improvements=2, score=6117->6119, near_progress=0.8123->0.8137, covered_delta=1.2 m, final_union_audit_delta=0 m, deadline=open.
'''
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'run.log'
    p.write_text(text)
    got = parse_log(p)
assert got['wiggle_seen'] is True
assert got['wiggle_baseline_match'] is True
assert got['wiggle_movable_edge_guards'] == 317
assert got['wiggle_frozen_vertices'] == 683
assert got['wiggle_trials'] == 2450
assert got['wiggle_completed_rows'] == 2450
assert got['wiggle_accepted'] == 19
assert got['wiggle_momentum_continuations'] == 7
assert got['wiggle_score_improvements'] == 2
assert got['wiggle_score_start'] == 6117
assert got['wiggle_score_end'] == 6119
assert abs(got['wiggle_near_progress_end'] - 0.8137) < 1e-12
print('RINGARC35 wiggle conclusion telemetry: PASS')
