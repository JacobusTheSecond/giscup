# GIS Cup visibility solver — parallel large-cache preparation and anchor expansion

This project keeps the fast local CDT visibility walk, visibility-induced candidates, polygon-epoch hot-candidate 1-swap search, anchor-expanded restricted CP-SAT, final polishing, and live plotting.

The large-cache preparation path and conditional partner generation now use OpenMP wherever work can be partitioned without shared mutation. The restricted multi-swap phase retains explicit controls for exchange size, cadence, and rounds per scheduled cardinality. The 1-swap implementation remains hot-candidate-only; selected guards are not assigned hot/cold state.

## Parallel large-cache preparation

With `--cache-memory expanded`, post-build preparation now does the following:

- writes a newly built compressed cache in a background thread while evaluation structures are prepared;
- counts CSR row sizes in parallel;
- allocates the expanded sample array without a separate zero-fill pass;
- decodes compressed rows directly into disjoint CSR slices in parallel;
- hashes encoded visibility rows in parallel before deterministic exact deduplication;
- builds candidate-to-polygon rows in parallel and reduces their membership count in parallel.

The compressed-cache write is joined before optimization starts, so write failures are still reported synchronously. No new command-line option is required; these stages use `--threads`/`OMP_NUM_THREADS`.

## Parallel conditional partner generation

Restricted multi-swap anchors are now scored concurrently. Each worker:

1. constructs the provisional anchor replacement state;
2. samples polygons and candidate partners with the same deterministic per-anchor seed;
3. evaluates and ranks conditional partners independently.

The expensive section performs no shared writes. Results are merged serially in original anchor order, duplicate partners are skipped deterministically, and later anchors fall back to their next-best unique partner. Nested OpenMP teams are disabled inside each anchor evaluator, so `--threads 10` means up to ten concurrent anchors rather than ten anchors each spawning ten more workers.

Anchor generation uses per-worker scratch memory. On memory-constrained systems, lowering `--threads` reduces the peak; on the 502 GB machine discussed for this dataset, ten workers should be reasonable.

## Boundary-slid visibility candidates

Only the collinearities created by a generating vertex's two incident edges are perturbed:

- every original polygon vertex computes its boundary visibility polygon;
- the vertices of those visibility polygons are the candidate set;
- for each of the two vertices contributed by the source vertex's adjacent edges, the candidate stays on the boundary edge reached at the far endpoint;
- the generating edge's normal line chooses the tangential orientation, and the candidate is moved by `--boundary-epsilon` along that landing edge;
- every other visibility-polygon vertex is canonically projected onto its known generating edge, without any epsilon-radius snapping;
- optional candidate subdivision points remain exactly on their source edge and are never slid;
- boundary discretization points created from final-candidate visibility remain unchanged and are never slid.

Every final candidate is therefore treated as a boundary query. Ordinary visibility vertices are reconstructed from their generating edge and projected edge parameter; slid and subdivision candidates carry the same canonical edge metadata directly. The independent CGAL verifier reconstructs each such query from exact source-edge endpoints rather than trusting a visibility-walk coordinate that may lie a few ulps off the boundary.

Same-edge identity is lazy. The visibility walk remains in the fast inexact kernel and records only construction provenance. A conservative edge-parameter interval first identifies plausible duplicates; exact CGAL ray/edge parameters are materialized only for those overlapping buckets. Repeated slid constructions already carry their canonical landing-edge parameter. Distinct exact parameters are never merged, and boundary versus deliberately slid candidates remain separate identity classes. No epsilon-radius snapping or coordinate averaging is performed.

`--boundary-epsilon` is not reused as a CDT classification, collinearity, breakpoint-merging, or sample-inclusion tolerance. It controls only the distance travelled along the designated landing edges.

With `--write-samples`, `boundary_samples.geojson` includes `edge_slide`, `edge_sid`, `edge_t`, the original visibility vertex in `anchor_x/anchor_y`, and the travelled distance in `offset_m`. Subdivision candidates have `edge_slide=0`, an exact `edge_sid/edge_t`, and `offset_m=0`. The legacy `offset` field remains zero because these points are not moved off the boundary.

The program revision is `PIPELINE1`, and the cache algorithm revision is `PIPE001`; existing visibility caches are rejected. Rebuild the cache on the first run. Use `./build/gis_cup_visibility --version` to verify that the expected executable is being run.

## Candidate and coverage generation

The executable enforces this three-stage pipeline:

1. **Seed visibility.** Compute a visibility polygon from every original polygon vertex.
2. **Final boundary candidates.** Collect the first-stage visibility-polygon vertices within `--vertex-visibility-distance`, include the original polygon vertices, selectively slide only incident-edge collinearity vertices along their landing edge, and optionally add unchanged on-edge subdivisions. This completes the guard-candidate set.
3. **Coverage discretization.** Compute visibility from every final guard candidate. The vertices of these second-round visibility polygons are used only to partition original polygon edges into weighted coverage intervals. They are never appended as new guard candidates.

The program performs runtime invariant checks between these stages. Before Stage 3, every sample must be a zero-weight boundary candidate. After Stage 3, the candidate-ID vector must be unchanged and every newly appended sample must be an unperturbed, positive-weight coverage interval. A successful run prints a `Three-stage pipeline audit` line ending in `zero recursive candidates`.

`--candidate-subdivisions N` divides each interval between consecutive first-stage candidate locations on an original edge into `N` equal parts. `1` adds no points; `3` adds two unchanged interior candidates per interval. Subdivision points participate in Stage 3 exactly like all other final candidates.

The legacy `--boundary-spacing` and `--candidate-spacing` options are accepted but ignored.

## Independent CGAL verification

After optimization, the solver now runs an independent continuous-boundary verifier by default. It builds a separate CGAL arrangement with `Exact_predicates_exact_constructions_kernel`, computes non-regularized triangular-expansion visibility for every selected guard, intersects those visibility boundaries with every original source edge, and unions the covered source-edge parameter intervals before scoring. This pass does not use the sampled visibility cache.

It writes:

- `cgal_verification.csv`: per-polygon CGAL-visible length, fraction, threshold margin, qualification, cached result, and delta;
- `cgal_verification.txt`: solution cardinality/uniqueness/free-space status, verified guard count, real score, cached score, score delta, and runtime.

The program exits with status 2 when the selected solution is structurally infeasible. Use `--no-cgal-verify` only when deliberately skipping this final pass. Exact CGAL predicates, constructions, intersections, and interval unions are retained through the coverage calculation; the final source-edge Euclidean lengths and threshold comparison use the same `double` length model as the optimizer.

## Restricted multi-swap scheduling

The restricted CP-SAT neighborhood runs only after the configured add/cardinality rounds:

- `--multi-swap-max-new A` allows at most `A` newcomers in the returned guard set.
- `--multi-swap-every B` runs the phase only after every `B`th add round. For example, `B=4` schedules it at cardinalities 4, 8, 12, and so on.
- `--multi-swap-max-rounds N` caps CP-SAT attempts at each scheduled cardinality. `0` keeps trying after improvements until the first miss.

The phase runs only when the current cardinality is greater than `A`. Each accepted restricted move returns to a complete hot-candidate 1-swap descent. When the restricted solver misses or reaches its round cap, construction advances to the next add.

Example:

```bash
--multi-swap-max-new 3 \
--multi-swap-every 4 \
--multi-swap-max-rounds 3
```

This means: consider up-to-3 exchanges every fourth add round, with at most three restricted CP-SAT attempts at each scheduled cardinality.

## Build on Linux with micromamba

The CMake project includes the Eigen config preference and the conda-forge OR-Tools 9.6 COIN-OR imported-target workaround, so the normal configure command is sufficient:

```bash
micromamba activate gisvis
rm -rf build
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$CONDA_PREFIX"
cmake --build build --parallel 10
```

The expected environment packages are `cxx-compiler`, `cmake`, `ninja`, `gdal`, `boost-cpp`, `cgal-cpp`, `eigen=3.4.*`, `pkg-config`, `coin-or-cbc`, and `ortools-cpp`.

## Build on macOS/Homebrew

```bash
xcode-select --install
brew install cmake ninja gdal boost cgal or-tools libomp

rm -rf build

LIBOMP_PREFIX="$(brew --prefix libomp)"
ORTOOLS_PREFIX="$(brew --prefix or-tools)"
SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"

cmake \
  -S . \
  -B build \
  -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER="$(xcrun --find clang++)" \
  -DCMAKE_OSX_SYSROOT="$SDKROOT" \
  -DOpenMP_CXX_FLAGS="-Xpreprocessor -fopenmp -I$LIBOMP_PREFIX/include" \
  -DOpenMP_CXX_LIB_NAMES="omp" \
  -DOpenMP_omp_LIBRARY="$LIBOMP_PREFIX/lib/libomp.dylib" \
  -DCMAKE_EXE_LINKER_FLAGS="-L$LIBOMP_PREFIX/lib -Wl,-rpath,$LIBOMP_PREFIX/lib" \
  -DCMAKE_BUILD_RPATH="$LIBOMP_PREFIX/lib" \
  -DCMAKE_INSTALL_RPATH="$LIBOMP_PREFIX/lib" \
  -DCMAKE_PREFIX_PATH="$(brew --prefix);$LIBOMP_PREFIX;$ORTOOLS_PREFIX"

cmake --build build --parallel 8
```

## Complete run command

```bash
mkdir -p output

OPENBLAS_NUM_THREADS=1 \
OMP_NUM_THREADS=10 \
OMP_DYNAMIC=FALSE \
OMP_PROC_BIND=close \
OMP_PLACES=cores \
./build/gis_cup_visibility \
  --input data/data.geojson \
  --k 50 \
  --t 0.75 \
  --m 0.75 \
  --vertex-visibility-distance 50.0 \
  --bbox-padding 20.0 \
  --boundary-epsilon 0.000001 \
  --threads 10 \
  --cache-memory expanded \
  --progress-every 1000 \
  --snapshot-every 100 \
  --max-swap-passes 0 \
  --multi-swap-max-new 3 \
  --multi-swap-every 1 \
  --multi-swap-max-rounds 3 \
  --multi-swap-extra-count 50 \
  --multi-swap-time-limit 15 \
  --multi-swap-partners-per-anchor 1 \
  --multi-swap-anchor-polygon-samples 8 \
  --multi-swap-anchor-candidate-samples 256 \
  --multi-swap-anchor-seed 1 \
  --polish-max-rounds 0 \
  --polish-time-limit 300 \
  --polish-extra-count 100 \
  --polish-relative-gap 0 \
  --polish-workers 10 \
  --polish-solver-log \
  --timeline output/optimization_timeline.csv \
  --force-rebuild-cache \
  --cache output/buildings_visibility_vertices.viscache \
  --output-dir output
```

Remove `--force-rebuild-cache` after the first successful cache build.

## Live plot

In another terminal:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-plot.txt
./live_plot.sh
```

Or run the plotter directly:

```bash
python3 plot_optimization_timeline.py \
  output/optimization_timeline.csv \
  --live \
  --interval 5
```
