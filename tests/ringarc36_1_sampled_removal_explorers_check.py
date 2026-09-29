#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/main.cpp").read_text()
WORKER = (ROOT / "run_v31_vertex_cell.sbatch").read_text()
START = (ROOT / "start_v31_24h.sh").read_text()
SUP = (ROOT / "submit_v31_supervisor.py").read_text()

# Default exact evaluator remains the default: subset is optional and null.
assert "const std::vector<std::size_t>* removal_slot_subset = nullptr" in SRC
assert "if (removal_slot_subset != nullptr)" in SRC
assert "for (const std::size_t slot : context.default_removal_order)" in SRC
assert "for (const std::size_t slot : special_slots)" in SRC

# Explorer activation is post-k only and tied to the hottest/highest-id groups.
assert 'phase_label.rfind("post_k_", 0) == 0' in SRC
assert "group_id >= group_count - sampled_removal_group_count" in SRC
assert "options.sim_anneal_sampled_removal_low_loss_anchors" in SRC
assert "splitmix64(rng() + attempts_to_fill" in SRC
assert "sampled_removal_explorer ||\n                        options.sim_anneal_pair_cache_size == 0" in SRC

# Production defaults: two hot vertex explorers, four after edge handoff, 8 slots with 2 anchors.
for text in (WORKER, START, SUP):
    for key in (
        "GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS",
        "GRID_EDGE_SAMPLED_REMOVAL_GROUPS",
        "GRID_SAMPLED_REMOVAL_SLOTS",
        "GRID_SAMPLED_REMOVAL_ANCHORS",
    ):
        assert key in text, (key, text[:80])
assert "GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS=${GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS:-2}" in WORKER
assert "GRID_EDGE_SAMPLED_REMOVAL_GROUPS=${GRID_EDGE_SAMPLED_REMOVAL_GROUPS:-4}" in WORKER
assert "GRID_SAMPLED_REMOVAL_SLOTS=${GRID_SAMPLED_REMOVAL_SLOTS:-8}" in WORKER
assert "GRID_SAMPLED_REMOVAL_ANCHORS=${GRID_SAMPLED_REMOVAL_ANCHORS:-2}" in WORKER

print("RINGARC36.1 sampled-removal explorer invariants: PASS")
