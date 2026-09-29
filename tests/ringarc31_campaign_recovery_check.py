#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='r31-recovery-') as td:
    td=Path(td); camp=td/'campaign'; runs=camp/'runs'; runs.mkdir(parents=True)
    for ti,tau in enumerate((0.25,0.5,0.75)):
        for ki,k in enumerate((2,3,4)):
            tag=f't{ti}_k{k}'
            rd=runs/tag; rd.mkdir()
            coords=','.join(f'({i+0.1*ti:.17g}, {i+0.2*ki:.17g})' for i in range(k))
            (rd/'submission_block.txt').write_text(f'({tau:.17g}, {k})\n{coords}\n{100+ti*10+ki}\n')
            (rd/'submission_claim_slack.tsv').write_text('building_id\tmargin_m\tvisible_m\ttarget_m\n999\t-0.0005\t9.9995\t10\n')
    out=td/'out'
    subprocess.run([sys.executable,str(ROOT/'tools/collect_campaign_solutions.py'),'--campaign',str(camp),'--output-dir',str(out)],check=True,stdout=subprocess.PIPE,text=True)
    assert (out/'solutions.txt').is_file()
    manifest=json.loads((out/'solution_manifest.json').read_text())
    assert manifest['schema']=='ringarc31.recovered-campaign-solutions.v1'
    assert len(manifest['cells'])==9
    for cell in manifest['cells']:
        assert cell['archive_claim_slack'].startswith('claim_slack/')
        assert (out/cell['archive_claim_slack']).is_file()
print('RINGARC31 campaign recovery + claim-slack packaging: PASS')
