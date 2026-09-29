#!/usr/bin/env python3
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()

# All proven + new destroy modes are present in one nine-mode rotation.
for needle in [
    'constexpr std::size_t ruin_mode_count = 9U;',
    'mode_name = "low-loss";',
    'mode_name = "spatial-cluster";',
    'mode_name = "frontier-building";',
    'mode_name = "coverage-neighborhood";',
    'mode_name = "uniform-random";',
    'mode_name = "building-star";',
    'mode_name = "stochastic-low-loss";',
    'mode_name = "dispersed-random";',
    'mode_name = "visibility-graph-cluster";',
]:
    assert needle in SRC, needle

# Pure random really is an unbiased shuffle of selected slots; stochastic low-loss
# uses weighted sampling without replacement keys; dispersed random is an
# incremental farthest-point traversal; graph cluster grows through the existing
# reverse candidate/polygon CSR.
for needle in [
    'std::shuffle(removal_order.begin(), removal_order.end(), rng);',
    'const double key = -std::log(unit01(rng)) / weight;',
    'nearest_distance[slot] = std::min(',
    'farthest_distance = nearest_distance[slot];',
    'candidate_polygon_reverse_index.candidates(polygon_id)',
    'neighbor_score[found->second]',
]:
    assert needle in SRC, needle

# Building-star is the only new mode allowed to derive its destroy count from
# structural star cardinality. The existing global min/max remain authoritative.
for needle in [
    'forced_ruin_count = std::clamp<std::size_t>(',
    'star_size, minimum_ruin, maximum_ruin);',
    'std::size_t ruin_count = forced_ruin_count.value_or(base_ruin);',
    'if (!forced_ruin_count.has_value()) {',
]:
    assert needle in SRC, needle

# New random/stochastic/dispersed modes remain at base size; only graph-cluster
# joins the existing spatial/frontier moderate +50% treatment.
assert 'if (mode == 1U || mode == 2U || mode == 8U)' in SRC
assert 'mode == 4U || mode == 6U || mode == 7U' not in SRC

# Resume/import duplicate selected guards remain visible to building-star even if
# they have no compact forward CSR row.
for needle in [
    'cache.for_each_arc(candidate, [&](std::uint32_t begin, std::uint32_t)',
    'scene_sample_polygon_id(scene, begin) == polygon_id',
]:
    assert needle in SRC, needle

# Most important competition-week guardrail: once destruction is chosen, the
# entire recreate/refill/acceptance slice must remain byte-identical to the frozen v34.0.1 baseline
# inherited by this release.
start_marker = '            // Destruction must happen before we decide which candidates are\n'
end_marker = '        const bool improved = better_search_quality(\n'
start = SRC.index(start_marker)
end = SRC.index(end_marker, start)
recreate_slice = SRC[start:end]
expected = 'cb0bdb34841780bf137c193fe35c7b4d99491d664535d412af9df3fecd276733'
actual = hashlib.sha256(recreate_slice.encode()).hexdigest()
assert actual == expected, (actual, expected)

print('RINGARC33 nine-mode ruin diversity invariants: PASS')
