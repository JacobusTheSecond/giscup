#!/usr/bin/env python3
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import submit_v31_supervisor as supervisor

original = supervisor.run_capture
try:
    def fake_collision(command, timeout=None):
        assert 'JobIDRaw,State,ExitCode,Elapsed,NodeList,MaxRSS' in command
        return 0, (
            '7933_0|CANCELLED|0:0|00:00:31|hendrixgpu12fl|1G\n'
            '7933|COMPLETED|0:0|05:19:43|hendrixfut03fl|42G\n'
        )
    supervisor.run_capture = fake_collision
    snap = supervisor.accounting_snapshot('7933')
    assert snap == {
        'state': 'COMPLETED', 'exit': '0:0', 'elapsed': '05:19:43',
        'node': 'hendrixfut03fl', 'maxrss': '42G'
    }, snap

    def fake_only_collision(command, timeout=None):
        return 0, '7933_0|CANCELLED|0:0|00:00:31|hendrixgpu12fl|1G\n'
    supervisor.run_capture = fake_only_collision
    assert supervisor.accounting_snapshot('7933') is None
finally:
    supervisor.run_capture = original

print('RINGARC34 exact sacct JobIDRaw matching: PASS')
