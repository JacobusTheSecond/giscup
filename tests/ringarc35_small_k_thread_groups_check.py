#!/usr/bin/env python3
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORKER = (ROOT / "run_v31_vertex_cell.sbatch").read_text()

import submit_v31_supervisor as sup

for key in [
    "GRID_SA_GROUPS", "GRID_ACTIVE_SA_SEEDS", "GRID_EXACT_SWAP_LANE",
    "GRID_EXACT_SWAP_LANE_CPU_FRACTION", "GRID_EXACT_SWAP_LANE_MIN_THREADS",
    "GRID_EXACT_SWAP_LANE_MAX_THREADS",
]:
    os.environ.pop(key, None)

def values(k: int, cpus: int = 64):
    task = sup.Task(order=0, k=k, tau=0.25, tag=f"k{k}", min_cpus=16,
                    min_mem_mb=131072, max_cpus=cpus, max_mem_mb=131072)
    return sup.make_cell_export(task, {"GRID_ALL_K_VALUES": str(k)}, cpus)

# 64 CPUs with the normal 12.5% exact lane reserves 8 exact threads, leaving
# 56 usable SA threads.  The smoothstep is anchored at 16 groups for k=100 and
# reaches one group per usable SA thread at k=50 and below.
expected = {
    150: (16, 5),
    100: (16, 5),
    90: (20, 5),
    80: (30, 6),
    75: (36, 7),
    70: (42, 7),
    60: (52, 8),
    50: (56, 8),
    40: (56, 8),
}
last_groups = 0
for k in sorted(expected, reverse=True):
    vals = values(k)
    groups, seeds = expected[k]
    assert int(vals["GRID_SA_GROUPS"]) == groups, (k, vals["GRID_SA_GROUPS"])
    assert int(vals["GRID_ACTIVE_SA_SEEDS"]) == seeds, (k, vals["GRID_ACTIVE_SA_SEEDS"])
    if k <= 100:
        assert int(vals["GRID_SA_GROUPS"]) >= last_groups
        last_groups = int(vals["GRID_SA_GROUPS"])

# The endpoint is based on actual SA threads, not raw CPUs.  With 32 CPUs the
# exact lane is 4 threads and k=50 therefore resolves to 28 groups.
assert values(50, 32)["GRID_SA_GROUPS"] == "28"
# Below the exact-lane activation threshold, every CPU can host a group.
assert values(50, 16)["GRID_SA_GROUPS"] == "16"

# Explicit overrides remain authoritative.
os.environ["GRID_SA_GROUPS"] = "12"
os.environ["GRID_ACTIVE_SA_SEEDS"] = "6"
vals_override = values(50)
assert vals_override["GRID_SA_GROUPS"] == "12"
assert vals_override["GRID_ACTIVE_SA_SEEDS"] == "6"

src = (ROOT / "src/main.cpp").read_text()
assert "worker_thread_budget = total_search_thread_budget - exact_lane_threads;" in src
assert "std::min({options.sim_anneal_groups, worker_thread_budget" in src
assert "tiny_k_x * tiny_k_x * (3.0 - 2.0 * tiny_k_x)" in (ROOT / "submit_v31_supervisor.py").read_text()
assert "x * x * (3.0 - 2.0 * x)" in WORKER
assert "base_groups = min(16, sa_budget)" in WORKER
print("ringarc35_small_k_thread_groups_check: PASS")
