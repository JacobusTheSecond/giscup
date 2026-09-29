#!/usr/bin/env python3
"""Cheap competition-shape preflight.

Run on a compute node.  It does not decide visibility; it only catches input
features that the organizer evaluator rejects before geometry evaluation.
"""
from __future__ import annotations
import argparse, json, math, sys
from pathlib import Path


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('input', type=Path)
    ap.add_argument('--mode', choices=['warn','strict'], default='warn')
    ap.add_argument('--id-field', default='id')
    ap.add_argument('--report', type=Path)
    a=ap.parse_args()
    data=json.loads(a.input.read_text(encoding='utf-8'))
    errors=[]; warnings=[]
    if not isinstance(data, dict) or data.get('type')!='FeatureCollection':
        errors.append('root is not a GeoJSON FeatureCollection')
        features=[]
    else:
        features=data.get('features')
        if not isinstance(features,list):
            errors.append('FeatureCollection.features is not an array'); features=[]
    ids=set(); holes=0; nonpoly=0; missing_id=0; dup=0; malformed=0
    for i,f in enumerate(features):
        if not isinstance(f,dict): malformed+=1; continue
        props=f.get('properties') if isinstance(f.get('properties'),dict) else {}
        if a.id_field not in props:
            missing_id+=1
        else:
            sid=str(props[a.id_field])
            if sid in ids: dup+=1
            ids.add(sid)
        g=f.get('geometry') if isinstance(f.get('geometry'),dict) else {}
        if g.get('type')!='Polygon':
            nonpoly+=1; continue
        coords=g.get('coordinates')
        if not isinstance(coords,list) or not coords:
            malformed+=1; continue
        if len(coords)!=1: holes += max(0,len(coords)-1)
        for ring in coords:
            if not isinstance(ring,list) or len(ring)<4:
                malformed+=1; break
    if missing_id: errors.append(f'{missing_id} feature(s) missing properties.{a.id_field}')
    if dup: errors.append(f'{dup} duplicate properties.{a.id_field} value(s)')
    if nonpoly: errors.append(f'{nonpoly} non-Polygon feature(s)')
    if malformed: errors.append(f'{malformed} malformed Polygon/Feature record(s)')
    if holes:
        msg=f'{holes} interior ring(s)/hole(s); organizer evaluator currently rejects polygons with holes'
        (errors if a.mode=='strict' else warnings).append(msg)
    lines=[
        f'input={a.input.resolve()}', f'features={len(features)}', f'unique_ids={len(ids)}',
        f'holes={holes}', f'non_polygon={nonpoly}', f'missing_id={missing_id}',
        f'duplicate_id={dup}', f'malformed={malformed}', f'mode={a.mode}',
    ]
    lines += [f'warning={x}' for x in warnings]
    lines += [f'error={x}' for x in errors]
    lines.append('status=' + ('FAIL' if errors else 'PASS'))
    text='\n'.join(lines)+'\n'
    if a.report:
        a.report.parent.mkdir(parents=True,exist_ok=True); a.report.write_text(text)
    sys.stdout.write(text)
    return 2 if errors else 0

if __name__=='__main__':
    raise SystemExit(main())
