#!/usr/bin/env python3
"""Regression model for v36 exact interval -> compact coverage arc mapping."""
from bisect import bisect_left, bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()

# Synthetic ring split into nonuniform coverage intervals. Prefix is ring-local,
# exactly like compact_coverage_weight_prefix. Their midpoints are recoverable
# without retaining BoundaryPoint coverage records.
weights = [0.7, 1.1, 0.2, 2.0, 0.6, 1.4]
prefix=[]
x=0.0
for w in weights:
    x += w
    prefix.append(x)
mid=[]
prev=0.0
for p in prefix:
    mid.append((prev+p)/2.0)
    prev=p

# Segment occupies ring arclength [1.8, 3.8]. Exact visibility parameter
# interval [.10,.75] therefore covers midpoint arclength [2.0,3.3].
seg_begin=1.8
seg_len=2.0
lo=seg_begin+0.10*seg_len
hi=seg_begin+0.75*seg_len
compact_begin=bisect_left(mid,lo)
compact_end=bisect_right(mid,hi)
expected=[i for i,m in enumerate(mid) if lo <= m <= hi]
assert list(range(compact_begin,compact_end)) == expected

# Source must use exactly this lower/upper midpoint convention and offset compact
# coverage IDs by the (wiggle-local) selected guard count.
for needle in [
    'if (midpoint_arclength(middle) < lo_arclength) first = middle + 1U;',
    'if (midpoint_arclength(middle) <= hi_arclength) first = middle + 1U;',
    'visible_begin = scene_.candidates.size() + compact_begin;',
    'visible_end = scene_.candidates.size() + compact_end;',
    'scene_sample_polygon_id(scene_, previous.begin)',
    'scene_sample_ring_id(scene_, previous.begin)',
]:
    assert needle in SRC, needle

print('RINGARC36 compact wiggle interval mapping: PASS')
