# Test artifacts

- `two_squares.geojson`: two aligned square obstacles used for landing-edge slide and verifier smoke tests.
- `polygon_with_hole.geojson`: one polygon with an interior ring, used to verify landing-edge slides on exterior and interior rings.
- `edge_slide_two_squares.log`: end-to-end run showing sixteen boundary slides, lazy same-edge identity, unchanged coverage samples, and agreement with the CGAL verifier.
- `edge_slide_hole.log`: corresponding end-to-end run for a polygon with a hole.
- `landing_edge_slide_check.txt`: compact assertions for slide counts, boundary status, travelled distance, lazy exact-parameter counts, and verifier agreement.
- `edge_slide_syntax.log`: warnings-enabled C++20 syntax check; the empty file indicates no diagnostics.
- `edge_slide_build.log`: linked Clang build diagnostics; the empty file indicates no diagnostics.

Older logs are retained for comparison with the previous normal-offset and eager exact-identity implementations.

## Candidate subdivision regression

`subdivision_1_smoke.log` confirms that `--candidate-subdivisions 1` preserves the base visibility-vertex candidate set. `subdivision_3_smoke.log` and `subdivision_3_hole.log` confirm that `3` adds two unchanged on-edge candidates per source interval while leaving the designated incident-edge slide count unchanged.

## Canonical boundary-query regression

`canonical_boundary_hole_k10.log` checks ten selected guards on an exterior ring plus a hole, including vertex-face probing. `canonical_boundary_large_rotated_k20.log` checks twenty selected guards on non-axis-aligned edges at million-scale coordinates. In both runs every selected guard is accepted as a boundary/free-space query and the CGAL score matches the cache.
