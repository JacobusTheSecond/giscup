#!/usr/bin/env python3
from __future__ import annotations
import subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='r31-dash-') as td:
    camp=Path(td); (camp/'logs').mkdir(); (camp/'runs'/'t_demo').mkdir(parents=True); (camp/'edge_foundry').mkdir()
    (camp/'jobs.tsv').write_text('order\ttag\tk\ttau\tstate\tjob_id\tnode\tbasis_node\tcpus\tmem_gib\tattempt\tstartup_ok\treason\n1\tt_demo\t500\t0.5\tRUNNING\t123\tnode1\tnode1\t32\t256.0\t1\t1\tok\n')
    (camp/'logs'/'t_demo.log').write_text('=== PHASE 2A/3: exact-CGAL vertex construction ===\nSimulated annealing: best_score=101 current_score=99 primary_drawdown=2 escape_wins=1\n')
    cp=subprocess.run([sys.executable,str(ROOT/'tools/render_v31_dashboard.py'),str(camp)],check=True,text=True,stdout=subprocess.PIPE)
    text=cp.stdout
    assert 'FILE-ONLY renderer' in text
    assert 't_demo' in text and 'RUNNING' in text
    assert 'Simulated annealing' in text
print('RINGARC31 file-only dashboard smoke: PASS')
