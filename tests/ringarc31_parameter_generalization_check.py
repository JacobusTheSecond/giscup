#!/usr/bin/env python3
from pathlib import Path
import importlib.util, os, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('r31sup_generalization',ROOT/'submit_v31_supervisor.py')
assert spec and spec.loader
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
old=dict(os.environ)
try:
    os.environ['GRID_K_VALUES']='50,2371,8200'; os.environ['GRID_TAU_VALUES']='0.31,0.63,0.91'
    tasks=mod.task_list(); assert len(tasks)==9
    assert {(t.k,round(t.tau,12)) for t in tasks}=={(k,t) for k in (50,2371,8200) for t in (0.31,0.63,0.91)}
    assert len({t.tag for t in tasks})==9
    for k in (1,50,2371,8200,20000):
        p=mod.resource_policy(k)
        assert 8<=p['min_cpu']<=p['max_cpu']<=128
        assert p['min_mem']==128*1024 and p['max_mem']==128*1024
    # CPU-first worker placement: high-core fragments are not penalized by RAM.
    task=max((t for t in tasks if t.k==8200), key=lambda t:t.tau)
    node=mod.NodeFragment('fat','IDLE',128,8,120,512*1024,64*1024,440*1024,440*1024)
    env=mod.choose_initial_envelope(task,[node])
    assert env is not None and env.cpus==min(task.max_cpus,120) - (min(task.max_cpus,120)%4)
    assert env.mem_mb==128*1024
finally:
    os.environ.clear(); os.environ.update(old)
print('RINGARC31 arbitrary k/tau supervisor generalization: PASS')
