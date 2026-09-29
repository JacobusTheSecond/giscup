#!/usr/bin/env python3
from pathlib import Path
import json, subprocess, sys, tempfile
ROOT=Path(__file__).resolve().parents[1]
TOOL=ROOT/'tools/preflight_competition_input.py'
outer=[[0,0],[1,0],[1,1],[0,1],[0,0]]
hole=[[.2,.2],[.3,.2],[.2,.3],[.2,.2]]
def fc(features): return {'type':'FeatureCollection','features':features}
def feat(i,rings): return {'type':'Feature','properties':{'id':i},'geometry':{'type':'Polygon','coordinates':rings}}
with tempfile.TemporaryDirectory(prefix='r31-preflight-') as td:
    td=Path(td)
    ok=td/'ok.geojson'; ok.write_text(json.dumps(fc([feat(1,[outer]),feat(2,[outer])])))
    cp=subprocess.run([sys.executable,str(TOOL),str(ok),'--mode','strict'],text=True,stdout=subprocess.PIPE)
    assert cp.returncode==0 and 'status=PASS' in cp.stdout
    holes=td/'holes.geojson'; holes.write_text(json.dumps(fc([feat(1,[outer,hole])])))
    cp=subprocess.run([sys.executable,str(TOOL),str(holes),'--mode','warn'],text=True,stdout=subprocess.PIPE)
    assert cp.returncode==0 and 'warning=' in cp.stdout
    cp=subprocess.run([sys.executable,str(TOOL),str(holes),'--mode','strict'],text=True,stdout=subprocess.PIPE)
    assert cp.returncode==2 and 'status=FAIL' in cp.stdout
    dup=td/'dup.geojson'; dup.write_text(json.dumps(fc([feat(1,[outer]),feat(1,[outer])])))
    cp=subprocess.run([sys.executable,str(TOOL),str(dup),'--mode','warn'],text=True,stdout=subprocess.PIPE)
    assert cp.returncode==2 and 'duplicate' in cp.stdout
print('RINGARC31 competition-input preflight: PASS')
