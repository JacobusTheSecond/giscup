#!/usr/bin/env python3
from pathlib import Path
s = (Path(__file__).resolve().parents[1] / "src/main.cpp").read_text()
required = [
    "exact-cached-full-universe-1swap(per-group stepped-add cache",
    "per-group stepped-add cache",
    "repair_depth_cap_for_cardinality",
    "5.0 * small_k_factor",
    "group_stepped_add_cache",
    "group_pending_add_cache_changes",
    "find_best_cached_swap_for_state",
    "state_cache->refresh_changed",
    "state_cache->ordered_block_upper_bounds",
    "state_cache->candidate_upper_bound",
    "repair_search.cache_recomputed_candidates",
    "repair_search.pruned_candidate_slots",
        "SA cached live-repair context disagrees with incremental delta",
]
for needle in required:
    if needle not in s:
        raise SystemExit(f"missing v32 cached live-repair invariant: {needle}")
block = s[s.find("// RINGARC34: periodic live-state cached 1-swap repair."):
          s.find("if (current_quality > result.best_quality", s.find("// RINGARC34: periodic live-state cached 1-swap repair."))]
for forbidden in [
    "repair_hot_target",
    "repair_exact_shortlist",
    "constexpr std::size_t repair_scan_batch = 65536U;",
    "for (std::size_t begin = 0; begin < candidate_count;",
]:
    if forbidden in block:
        raise SystemExit(f"old bespoke/brute repair path survived: {forbidden}")
if "const bool partial_scan = repair_search.deadline_exhausted;" not in block or "++result.repair_full_scans_completed;" not in block:
    raise SystemExit("full/partial scan accounting guard missing")
# Each accepted primary SA move and each accepted repair move must invalidate the
# group's private stepped-add cache before another cached repair query can use it.
if block.count("group_pending_add_cache_changes.push_back") < 2:
    raise SystemExit("repair move cache invalidation missing")
pre = s[:s.find("// RINGARC34: periodic live-state cached 1-swap repair.")]
if pre.count("group_pending_add_cache_changes.push_back") < 2:
    raise SystemExit("accepted SA move cache invalidation missing")
restart = s[s.find("auto restart_from_best = [&]()"):
            s.find("auto begin_next_epoch", s.find("auto restart_from_best = [&]()"))]
for required_restart_guard in [
    "group_stepped_add_cache.emplace();",
    "group_pending_add_cache_changes.clear();",
]:
    if required_restart_guard not in restart:
        raise SystemExit(f"restart-from-best cache reset missing: {required_restart_guard}")
print("RINGARC34 per-group cached live SA repair with safe partial salvage: PASS")
