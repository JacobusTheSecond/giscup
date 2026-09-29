#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()
WORKER = (ROOT / 'run_v31_vertex_cell.sbatch').read_text()
REV = (ROOT / 'VERSION').read_text().strip()

assert f'PROGRAM_REVISION = "{REV}"' in SRC
assert f'GRID_EXPECTED_REVISION=${{GRID_EXPECTED_REVISION:-{REV}}}' in WORKER

# v36 wiggle must use the same exact-CGAL boundary engine as cache construction,
# including an arbitrary BoundaryPoint query for +/-1mm trials.
for needle in [
    'const ExactCgalBoundaryVisibilityEngine& engine,',
    'compute_visible_sample_arcs(\n        const BoundaryPoint& query_sample,',
    'compute_exact_source_intervals(query_sample, stats)',
    'guards[slot].arcs = engine.compute_visible_sample_arcs(',
    'engine.compute_visible_sample_arcs(trial.sample)',
    'build_arc_replacement_evaluation_context(',
    'build_arc_replacement_coverage_delta(',
    'evaluate_arc_replacement_quality(',
    'scoring=discrete-sample-arcs(score->near-progress; total tie-break)',
    'qualification_set_match=',
]:
    assert needle in SRC, needle

# The compact edge coverage partition is grafted onto the selected guards for
# wiggle; unlike v35, an empty dense sample vector can no longer fake a valid
# coverage index and reconstruct score zero.
for needle in [
    'RINGARC36 wiggle requires the authenticated edge compact coverage index',
    'scene.compact_coverage_weight_prefix = std::move(',
    'scene.compact_coverage_rings = std::move(',
    'scene.compact_total_sample_count =',
    'build_compact_coverage_block_index(scene);',
    'ExactCgalBoundaryVisibilityEngine wiggle_engine(',
    'wiggle_engine.refresh_scene_points();',
    'if (has_compact_coverage(scene_)) {',
    'Exact CGAL compact coverage index is incomplete',
]:
    assert needle in SRC, needle

# Central competition invariant: vertices are frozen and accepted moves are
# one-for-one replacements on the same canonical source edge.
for needle in [
    'if (guard.is_vertex || !guard.has_canonical_edge_parameter ||',
    'current.canonical_edge_segment, t)',
    'if (candidate.is_vertex || wiggle_position_occupied(guards, slot, candidate)) continue;',
    'RINGARC36 invariant failure: wiggle modified a vertex guard',
    'productive_wiggle_quality(',
]:
    assert needle in SRC, needle

quality = SRC[SRC.index('[[nodiscard]] bool productive_wiggle_quality('):]
quality = quality[:quality.index('\n}\n', quality.index('{')) + 3]
assert 'candidate.score > incumbent.score' in quality
assert 'candidate.near_progress > incumbent.near_progress + 1e-12' in quality
assert 'candidate.total' not in quality

# Worker wiggle is still edge-only and explicitly receives the authenticated
# edge cache/scene rather than silently falling back to the vertex universe.
assert '[ "$GRID_WIGGLE_ENABLE" != 0 ] && [ "$LAST_SEARCH_MODE" = "visibility" ]' in WORKER
assert '--execution-phase wiggle' in WORKER
assert '--cache "$EDGE_CACHE"' in WORKER
assert '--preprocessed-scene "$EDGE_COMPACT_SCENE"' in WORKER

print(f'{REV} exact edge-only threshold wiggle invariants: PASS')
