# Rebase notes

Baseline: `gis_visibility_exact_same_edge_identity.zip`.

Added in this revision:

- lazy exact same-edge identity: the inexact visibility walk records provenance, conservative parameter intervals form duplicate buckets, and EPECK parameters are constructed only for plausible equalities;
- program revision reporting through `--version` (`EDGESLIDE1`);
- incident-edge degeneracy candidates remain on the boundary and slide along the edge reached at the far endpoint;
- the generating edge's normal line selects the tangential orientation, but no candidate is displaced normal to the boundary;
- canonical landing-edge segment/parameter metadata for slid candidates;
- exact verifier reconstruction of slid guard coordinates from the landing edge and canonical parameter;
- `edge_slide`, `edge_sid`, and `edge_t` fields in `boundary_samples.geojson`;
- cache fingerprint revision `SLIDEEDG`.

Unchanged:

- final-candidate visibility creates the boundary discretization without perturbing or snapping its breakpoints;
- coverage interval representatives remain unchanged boundary midpoints;
- candidate-only polygon-epoch hot/cold 1-swap pruning;
- restricted CP-SAT objective and constraints;
- final post-k polishing model;
- independent CGAL continuous-boundary verification and exact acceptance checks.

## EDGESUBDIV1

`--candidate-subdivisions` is active again. After exact/lazy same-edge deduplication and the designated landing-edge slides, the solver partitions each source edge by the final base-candidate locations and inserts `N-1` unchanged interior candidates in each interval. Subdivision candidates are canonical on-edge queries and are never marked as incident-edge slides.

## EDGECANON1

All ordinary visibility-derived candidates are now projected onto their known generating source edge before candidate visibility and caching. The raw inexact visibility-walk point is retained only as construction provenance for lazy exact same-edge identity. This prevents a selected boundary guard from being a few floating-point ulps inside an obstacle. The CGAL verifier also probes the bisector of incident free-space normals when an individual normal lies exactly along the adjacent edge at a source vertex. Cache revision: `CANON001`.
