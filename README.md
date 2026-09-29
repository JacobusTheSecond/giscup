# RINGARC36.1 — sampled-removal explorers + hybrid edge SA + exact edge wiggle

Revision: `RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE`

RINGARC36.1 is the final surgical hybrid built from the v29/v35 evidence plus the v36 edge-breadth design:

- **Construction:** unchanged v36/v35.0.3 exact-best-removal grouped SA. **Post-k vertex:** 14/16 groups stay exact-best-removal; the two hottest groups use 8 sampled removals (2 low-loss anchors + 6 random) per decision. Vertex live repair, exact lane, portfolio, rotating partial repair, and resource policy remain intact.
- **Authenticated edge phase:** keeps v36 broad insertion screening (`12.5%`, clamped to `100000..500000`) and disables per-group live repair. 12/16 groups use sparse exact-best-removal; the four hottest groups use the same exact coverage evaluator restricted to 8 sampled removals, creating a genuinely different basin-exploration neighborhood.
- **Thermal shortlist:** broad edge proposals are exact-evaluated, then deterministically reduced to the strongest 1024 before the existing v35 softmax selector. Vertex proposal width remains 1024.
- **Wiggle:** fixes the v35 zero-score handshake by scoring baseline and +/-1 mm same-edge trials with the exact CGAL boundary engine over the authenticated compact coverage partition. Vertex guards remain frozen.

Canonical complete exact scans and v35.0.3 rotating-partial repair semantics are unchanged. The RINGARC34.0.1 exact-`JobIDRaw` supervisor hotfix and final-score reporting remain in place.

## Build on Hendrix

```bash
BUILD_JOB=$(./submit_build_v31.sh); echo "$BUILD_JOB"; tail -F "logs/r31-build-${BUILD_JOB}.log"
```

The build must report:

```text
Embedded revision: RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE
```

## 24-hour comparison run

```bash
INPUT=/path/to/la_large.geojson GRID_K_VALUES='500,5000,10000' GRID_TAU_VALUES='0.25,0.50,0.75' GRID_EDGE_SEARCH_FRACTION=0.30 GRID_WIGGLE_SECONDS=1800 GRID_SUBMISSION_CLAIM_SLACK_METERS=1e-8 ./start_v31_24h.sh
```

Operational filenames intentionally retain the historical `v31` names so proven competition-week plumbing is not renamed.
