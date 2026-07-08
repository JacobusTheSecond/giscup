# Validation

## Checks performed

- Full C++20 syntax compilation of `src/main.cpp` with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow` against GDAL, Boost, CGAL 6.0.1, and minimal OR-Tools API stubs. Only the two pre-existing warnings remain.
- Complete linked Clang C++20 build and end-to-end execution of the geometry, cache, local-search, and independent CGAL-verifier paths with OR-Tools-only phases disabled.
- `tests/two_squares.geojson` with `--boundary-epsilon 0.001`:
  - `--candidate-subdivisions 1` retained the 24 visibility-derived candidates and added no points;
  - `--candidate-subdivisions 3` produced 72 candidates: 24 visibility-derived candidates plus 48 unchanged interior subdivision points over 24 source intervals;
  - exactly 16 candidates were marked as incident-edge slides; all 48 subdivision candidates had `edge_slide=0` and `offset_m=0`;
  - no coverage-discretization point was marked as slid;
  - the independent CGAL verifier reported `real_score == cached_score`.
- `tests/polygon_with_hole.geojson` with `--candidate-subdivisions 3`:
  - produced the same 24 base + 48 subdivision candidate split;
  - exactly 16 incident-edge slides and zero slid subdivision/coverage points;
  - the independent CGAL verifier reported `real_score == cached_score`.
- Subdivision queries carry a canonical `(edge_sid, edge_t)` and are reconstructed directly on the exact source segment by the verifier.
- Every ordinary visibility-derived candidate now carries a canonical generating-edge parameter and its query coordinate is reconstructed on that edge before visibility caching. The raw walk coordinate is retained only for lazy exact identity.
- A ten-guard polygon-with-hole regression verifies source vertices, slid candidates, ordinary visibility vertices, and subdivision candidates in one run; all ten guards pass the independent CGAL free-space check.
- A twenty-guard large-coordinate, non-axis-aligned regression passes the exact verifier with `real_score == cached_score`.
- The source revision is `PIPELINE1`; after rebuilding, `./build/gis_cup_visibility --version` must report that value.
- The cache fingerprint revision is `PIPE001`, so caches from `EDGESUBDIV1` and earlier revisions are rejected.

The corresponding logs are:

- `tests/subdivision_1_smoke.log`
- `tests/subdivision_3_smoke.log`
- `tests/subdivision_3_hole.log`
- `tests/subdivision_check.txt`

## Not performed here

A production linked build with the real OR-Tools C++ library was not possible because that development package is unavailable in this container. The target project continues to link normally against `ortools::ortools` through CMake.

Additional logs:

- `tests/canonical_boundary_subdivision_3_smoke.log`
- `tests/canonical_boundary_hole_k10.log`
- `tests/canonical_boundary_large_rotated_k20.log`

## Three-stage pipeline assertions

- Stage 1 uses only original polygon vertices as seed visibility queries.
- Stage 2 finalizes the guard-candidate vector before any coverage samples exist.
- Stage 3 snapshots the final candidate IDs, computes second-round visibility breakpoints, and asserts that visibility evaluation did not mutate either the candidate vector or sample array.
- Appending weighted coverage intervals is followed by an assertion that the candidate IDs are unchanged and that every new sample is non-candidate, unperturbed, and has positive weight.
- The cache revision was bumped because the executable now records and enforces the clarified pipeline semantics.

## Checks specific to `PIPELINE1`

- Full `g++ -std=c++20 -fsyntax-only` compilation passed with the project warning flags, CGAL/GDAL headers, OpenMP, and the existing minimal OR-Tools API stubs.
- `tests/three_stage_pipeline_source_check.py` passed. It verifies Stage 1/2/3 ordering, confirms that Stage 3 snapshots the final candidate IDs, and confirms that neither the second-round visibility collector nor the Stage 3 block contains a candidate-appending path.
- The candidate geometry algorithms are unchanged from `EDGECANON1`; this revision adds explicit runtime invariants, stage naming, audit output, and a cache/revision marker.
- A newly linked end-to-end `PIPELINE1` binary was not produced in this container because compiling the full template-heavy translation unit exceeded the available execution window. The complete source did pass syntax compilation; the immediately preceding geometry implementation was exercised by the fixture runs listed above.
