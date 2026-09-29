#!/usr/bin/env python3
from pathlib import Path
import importlib.util, os, sys, tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('r31sup_resources',ROOT/'submit_v31_supervisor.py')
assert spec and spec.loader
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
old=dict(os.environ)
try:
    os.environ.pop('GRID_WORKER_MEM_GB',None)
    for k in (500,5000,10000):
        p=mod.resource_policy(k)
        assert p['min_mem']==128*1024==p['max_mem']
    os.environ['GRID_WORKER_MEM_GB']='160'
    assert mod.resource_policy(10000)['min_mem']==160*1024
    r=mod.FoundryRecord(job_id='123',attempt=1,state='RUNNING',node='n1',cpus=48,mem_gb=512)
    with tempfile.TemporaryDirectory() as d:
        mod.write_foundry_record(Path(d),r)
        txt=(Path(d)/'edge_foundry_job.tsv').read_text()
        assert 'node\tcpus\tmem_gib' in txt and '\tn1\t48\t512\t' in txt
finally:
    os.environ.clear(); os.environ.update(old)
print('RINGARC31 CPU-first 128GiB worker allocator/foundry telemetry: PASS')
