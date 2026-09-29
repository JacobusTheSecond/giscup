#!/usr/bin/env python3
from pathlib import Path
import math, random
ROOT=Path(__file__).resolve().parents[1]
src=(ROOT/'src/main.cpp').read_text()
worker=(ROOT/'run_v31_vertex_cell.sbatch').read_text()
for needle in [
    'constexpr double selector_temperature_multiplier = 4.0;',
    'for (const CachedPairProposal& proposal : proposals)',
    'const double gumbel = -std::log(-std::log(u));',
    'selector_delta / selector_temperature + gumbel',
    'temperature_hot_fraction',
    '0.15 + 0.50 * thermal_coldness',
]:
    assert needle in src, needle
assert 'tournament_size' not in src
assert 'GRID_SA_PROPOSAL_BATCH=${GRID_SA_PROPOSAL_BATCH:-1024}' in worker

# Mathematical regression for the same Gumbel-max selector family: high T is
# broad, low T is selective, and non-best proposals remain reachable.
def sample(temp, n=30000):
    qs=[0.0, 1.0, 2.0, 3.0]
    counts=[0]*len(qs)
    rng=random.Random(3105)
    for _ in range(n):
        best_i=0; best_key=-1e300
        for i,q in enumerate(qs):
            u=min(1.0-1e-15, max(1e-300, rng.random()))
            g=-math.log(-math.log(u))
            key=q/temp+g
            if key>best_key:
                best_key=key; best_i=i
        counts[best_i]+=1
    return [c/n for c in counts]
hot=sample(20.0)
cold=sample(0.5)
# Hot is close to broad/uniform; cold strongly prefers the best, but is stochastic.
assert max(hot)-min(hot) < 0.08, hot
assert cold[-1] > hot[-1] + 0.35, (hot,cold)
assert cold[-2] > 0.01, cold
print('RINGARC31.0.7 temperature-softmax selector: PASS')
