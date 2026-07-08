# Threading changes

This revision keeps the search objective and CP-SAT formulation unchanged. It only changes how independent preparation and pool-construction work is scheduled.

## Large-cache preparation

- A newly built compressed cache is written by `std::async` while CSR expansion, deduplication, and polygon-map construction proceed.
- CSR expansion uses a parallel row-count pass, a serial prefix sum, and a parallel decode pass into disjoint output ranges.
- The expanded sample array is allocated with `std::make_unique_for_overwrite`, avoiding an additional full-array zero-fill pass.
- Exact visibility-row hashes are computed in parallel; exact byte comparison and representative selection remain deterministic and serial.
- Candidate-to-polygon rows remain parallel, and their final membership count now uses an OpenMP reduction.

## Conditional partner generation

- Anchor provisional states, bounded polygon/candidate sampling, and conditional partner scoring run concurrently across anchors.
- Per-anchor random seeds are unchanged.
- Each worker owns its candidate deduplication stamps and all scoring scratch state.
- Conditional subset results are compact and aligned with the sampled candidate list rather than allocating candidate-count-sized output vectors for every anchor.
- Nested OpenMP teams are disabled inside anchor workers to avoid oversubscription.
- Ranked results are committed in original anchor order. Duplicate partners are skipped, and the later anchor falls back to its next-best unique candidate.

## Determinism

Given the same input, options, thread count, and toolchain, anchor sampling and final pool insertion order remain deterministic. Parallel completion order does not affect pool order.

## Memory

Anchor scoring uses per-worker provisional coverage and evaluator scratch arrays. Peak memory therefore grows with `--threads`. The discussed 502 GB machine has ample headroom for ten workers; on smaller machines, reduce `--threads`.
