#!/usr/bin/env python3
"""Create a compact iterative-feedback report for one RINGARC28 run.

The report is intentionally dependency-free and tolerant of partial/failed runs.
It writes JSON for machines, Markdown for humans/LLMs, and prints the Markdown
between stable markers so the tail of the Slurm log can be pasted directly into
an iterative development conversation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import socket
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_duration_text(text: str | None) -> float | None:
    if not text:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(s|sec|secs|min|mins|h|hr|hrs)\s*", text, flags=re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("min"):
        return 60.0 * value
    if unit.startswith("h"):
        return 3600.0 * value
    return value


def sha256_file(path: Path, max_bytes: int | None = None) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    remaining = max_bytes
    with path.open("rb") as handle:
        while True:
            size = 1024 * 1024
            if remaining is not None:
                if remaining <= 0:
                    break
                size = min(size, remaining)
            chunk = handle.read(size)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def parse_time_usage(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "present": path.is_file()}
    if not path.is_file():
        return result
    text = path.read_text(errors="replace")
    patterns = {
        "max_rss_kib": r"Maximum resident set size \(kbytes\):\s*(\d+)",
        "elapsed_wall": r"Elapsed \(wall clock\) time.*\):\s*(\S+)",
        "user_seconds": r"User time \(seconds\):\s*([0-9.]+)",
        "system_seconds": r"System time \(seconds\):\s*([0-9.]+)",
        "cpu_percent": r"Percent of CPU this job got:\s*([^\n]+)",
        "exit_status": r"Exit status:\s*(\d+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            value: Any = match.group(1).strip()
            if key in {"max_rss_kib", "exit_status"}:
                value = int(value)
            elif key in {"user_seconds", "system_seconds"}:
                value = float(value)
            result[key] = value
    if "max_rss_kib" in result:
        result["max_rss_gib"] = result["max_rss_kib"] / (1024.0 * 1024.0)
    return result


def parse_timeline(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "present": path.is_file(),
        "rows": 0,
        "accepted_moves": 0,
        "accepted_by_phase": {},
        "score_gain_by_phase": {},
        "first_k_seconds": None,
        "best_score": None,
        "best_score_seconds": None,
        "final_score": None,
        "final_covered_boundary_m": None,
        "elapsed_seconds": None,
    }
    if not path.is_file():
        return result

    phase_moves: Counter[str] = Counter()
    phase_first_score: dict[str, int] = {}
    phase_last_score: dict[str, int] = {}
    best_score = -10**18
    best_time: float | None = None
    final_row: dict[str, str] | None = None
    with path.open(newline="", errors="replace") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            result["rows"] += 1
            final_row = row
            phase = row.get("phase", "") or "unknown"
            event = row.get("event", "")
            score = safe_int(row.get("real_score_fixed_t"))
            elapsed = safe_float(row.get("elapsed_seconds"))
            cardinality = safe_int(row.get("cardinality"))
            k = safe_int(row.get("k"))
            if score is not None:
                phase_first_score.setdefault(phase, score)
                phase_last_score[phase] = score
                if score > best_score:
                    best_score = score
                    best_time = elapsed
            if event == "accepted_move":
                result["accepted_moves"] += 1
                phase_moves[phase] += 1
            if (
                result["first_k_seconds"] is None
                and cardinality is not None
                and k is not None
                and cardinality >= k
            ):
                result["first_k_seconds"] = elapsed

    result["accepted_by_phase"] = dict(sorted(phase_moves.items()))
    result["score_gain_by_phase"] = {
        phase: phase_last_score[phase] - phase_first_score[phase]
        for phase in sorted(phase_last_score)
    }
    if best_score > -10**18:
        result["best_score"] = best_score
        result["best_score_seconds"] = best_time
    if final_row:
        result["final_score"] = safe_int(final_row.get("real_score_fixed_t"))
        result["final_covered_boundary_m"] = safe_float(
            final_row.get("covered_boundary_m")
        )
        result["elapsed_seconds"] = safe_float(final_row.get("elapsed_seconds"))
    return result


def parse_verification(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "cgal_verification.txt"
    csv_path = run_dir / "cgal_verification.csv"
    values = read_key_values(summary_path)
    marker_path = run_dir / "VERIFIED_OK.tsv"
    result: dict[str, Any] = {
        "summary_path": str(summary_path),
        "gate_marker_path": str(marker_path),
        "gate_marker_present": marker_path.is_file(),
        "gate_marker": marker_path.read_text(errors="replace").strip()
            if marker_path.is_file() else None,
        "csv_path": str(csv_path),
        "present": summary_path.is_file(),
        "feasible": values.get("feasible"),
        "real_score": safe_int(values.get("real_score")),
        "preverify_cached_score": safe_int(values.get("preverify_cached_score")),
        "preverify_score_delta": safe_int(values.get("preverify_score_delta")),
        "selected": safe_int(values.get("selected")),
        "expected_k": safe_int(values.get("expected_k")),
        "near_threshold_polygons": safe_int(values.get("near_threshold_polygons")),
        "runtime_seconds": safe_float(values.get("runtime_seconds")),
        "violations": [
            line.split("=", 1)[1]
            for line in summary_path.read_text(errors="replace").splitlines()
            if line.startswith("violation=")
        ] if summary_path.is_file() else [],
    }

    bands = {"within_1mm": 0, "within_1cm": 0, "within_10cm": 0, "within_1m": 0}
    nearest_deficits: list[dict[str, Any]] = []
    if csv_path.is_file():
        with csv_path.open(newline="", errors="replace") as handle:
            for row in csv.DictReader(handle):
                margin = safe_float(row.get("threshold_margin_m"))
                if margin is None:
                    continue
                absolute = abs(margin)
                if absolute <= 0.001:
                    bands["within_1mm"] += 1
                if absolute <= 0.01:
                    bands["within_1cm"] += 1
                if absolute <= 0.1:
                    bands["within_10cm"] += 1
                if absolute <= 1.0:
                    bands["within_1m"] += 1
                if margin < 0.0:
                    nearest_deficits.append({
                        "polygon_id": safe_int(row.get("polygon_id")),
                        "source_fid": row.get("source_fid"),
                        "deficit_m": -margin,
                        "exact_fraction": safe_float(row.get("exact_fraction")),
                    })
        nearest_deficits.sort(key=lambda item: item["deficit_m"])
    result["threshold_margin_bands"] = bands
    result["nearest_unqualified"] = nearest_deficits[:25]
    return result


def parse_log(log_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(log_path),
        "present": log_path.is_file(),
        "candidate_count": None,
        "candidate_counts": [],
        "coverage_intervals": None,
        "edge_pool_accepted": False,
        "edge_pool_auth_failed": False,
        "expanded_universe_size": None,
        "remapped_restart_seeds": 0,
        "sa_phases": 0,
        "sa_improvements": 0,
        "sa_seed_allocations": [],
        "sa_group_summaries": 0,
        "sa_downhill_attempted": 0,
        "sa_downhill_accepted": 0,
        "sa_downhill_acceptance": None,
        "sa_reheats": 0,
        "sa_initial_temperature_min": None,
        "sa_initial_temperature_max": None,
        "sa_temperature_roles": {},
        "sa_removal_modes": {},
        "sa_current_best_divergence_samples": 0,
        "sa_current_below_best_samples": 0,
        "sa_phase_seconds": [],
        "sa_phase_seconds_by_label": {},
        "post_k_sa_assigned_seconds": [],
        "repair_rounds_attempted": 0,
        "repair_full_scans": 0,
        "repair_partial_scans": 0,
        "repair_partial_salvaged": 0,
        "repair_admission_skips": 0,
        "repair_rotating_partial_scans": 0,
        "repair_rotating_blocks_considered": 0,
        "repair_rotating_elite_prefix_blocks": 0,
        "repair_rotating_last_start_rank": None,
        "repair_rotating_next_rank": None,
        "repair_evaluated_candidates": 0,
        "repair_cached_pruned": 0,
        "repair_cache_recomputed": 0,
        "repair_scan_seconds": 0.0,
        "repair_max_scan_seconds": 0.0,
        "repair_max_predicted_scan_seconds": 0.0,
        "repair_max_grace_seconds": 0.0,
        "ruin_calls": 0,
        "ruin_improvements": 0,
        "ruin_attempts": 0,
        "ruin_destroyed_min": None,
        "ruin_destroyed_max": None,
        "ruin_stagnation_level_max": 0,
        "ruin_modes": {},
        "crossover_calls": 0,
        "crossover_improvements": 0,
        "exploration_seeds_queued": 0,
        "exploration_seeds_consumed": 0,
        "exploration_seeds_pending": 0,
        "exploration_seed_events": [],
        "exploration_score_drop_max_observed": 0,
        "exploration_distance_min": None,
        "exploration_distance_max": None,
        "one_swap_moves": 0,
        "portfolio_cycles": 0,
        "portfolio_stagnation_max": 0,
        "checkpoints": 0,
        "last_checkpoint_sequence": None,
        "last_checkpoint_score": None,
        "operator_stats": {},
        "exact_canonical_verifier_seen": False,
        "legacy_wiggle_seen": False,
        "wiggle_seen": False,
        "wiggle_baseline_match": None,
        "wiggle_movable_edge_guards": None,
        "wiggle_frozen_vertices": None,
        "wiggle_trials": 0,
        "wiggle_completed_rows": 0,
        "wiggle_accepted": 0,
        "wiggle_momentum_continuations": 0,
        "wiggle_score_improvements": 0,
        "wiggle_score_start": None,
        "wiggle_score_end": None,
        "wiggle_near_progress_start": None,
        "wiggle_near_progress_end": None,
        "errors": [],
    }
    if not log_path.is_file():
        return result
    text = log_path.read_text(errors="replace")

    candidate_values: list[int] = []
    candidate_patterns = [
        r"RINGARC(?:27|28) exact (?:vertex|boundary) candidate universe:\s*(\d+)",
        r"Exact candidate discretization ready:\s*(\d+)\s+candidates",
        r"Compact scene load complete:.*?\b(\d+)\s+candidates",
        r"canonical guards remapped into\s*(\d+)-candidate universe",
    ]
    for pattern in candidate_patterns:
        candidate_values.extend(int(x) for x in re.findall(pattern, text, flags=re.I))
    result["candidate_counts"] = candidate_values
    if candidate_values:
        result["candidate_count"] = candidate_values[-1]

    interval_patterns = [
        r"(?:induced|produced)\s+(\d+)\s+exact weighted boundary intervals",
        r"Exact candidate discretization ready:\s*\d+\s+candidates and\s*(\d+)\s+weighted boundary intervals",
        r"Compact scene load complete:.*?\+\s*(\d+)\s+compact coverage intervals",
        r"(\d+)\s+weighted intervals appended",
    ]
    interval_values: list[int] = []
    for pattern in interval_patterns:
        interval_values.extend(int(x) for x in re.findall(pattern, text, flags=re.I))
    if interval_values:
        result["coverage_intervals"] = interval_values[-1]

    result["edge_pool_accepted"] = (
        "edge foundry accepted" in text.lower()
        or "switching the final search slice to the extended candidate universe" in text.lower()
    )
    result["edge_pool_auth_failed"] = "edge foundry manifest failed authentication" in text.lower()
    remap_matches = re.findall(
        r"canonical guards remapped into\s*(\d+)-candidate universe.*?"
        r"(\d+)\s+diverse restart seed\(s\) were also remapped",
        text, flags=re.I | re.S,
    )
    if remap_matches:
        result["expanded_universe_size"] = int(remap_matches[-1][0])
        result["remapped_restart_seeds"] = int(remap_matches[-1][1])
    elif candidate_values and result["edge_pool_accepted"]:
        result["expanded_universe_size"] = max(candidate_values)

    result["sa_phases"] = len(re.findall(r"Simulated annealing phase \[", text))
    result["sa_improvements"] = len(re.findall(r"accepted best improvement", text, flags=re.I))
    phase_by_label: dict[str, list[float]] = defaultdict(list)
    for match in re.finditer(
        r"Simulated annealing phase \[(.*?)\] cardinality=\d+: time=([0-9.]+)\s*(s|min|h)",
        text, flags=re.I,
    ):
        seconds = parse_duration_text(match.group(2) + " " + match.group(3))
        if seconds is None:
            continue
        result["sa_phase_seconds"].append(seconds)
        phase_by_label[match.group(1)].append(seconds)
    result["sa_phase_seconds_by_label"] = {
        label: {
            "count": len(values),
            "min_seconds": min(values),
            "max_seconds": max(values),
            "avg_seconds": sum(values) / len(values),
        }
        for label, values in sorted(phase_by_label.items()) if values
    }
    for match in re.finditer(
        r"Portfolio cycle \d+: budget=.*?, exact-1-swap=.*?, SA=([0-9.]+)\s*(s|min|h)",
        text, flags=re.I,
    ):
        seconds = parse_duration_text(match.group(1) + " " + match.group(2))
        if seconds is not None:
            result["post_k_sa_assigned_seconds"].append(seconds)
    allocation_pattern = re.compile(
        r"SA active-seed allocation \(stagnation=(\d+), diversity_level=(\d+)\):\s*(.*?)\. Existing group count",
        flags=re.I,
    )
    for match in allocation_pattern.finditer(text):
        allocation_text = match.group(3)
        groups = []
        for group_match in re.finditer(
            r"(\d+) group\(s\) from (.*?) score=(-?\d+)(?:,|$)",
            allocation_text,
        ):
            source = group_match.group(2).strip()
            groups.append({
                "groups": int(group_match.group(1)),
                "source": source,
                "score": int(group_match.group(3)),
                "role": "incumbent" if source == "incumbent" else "challenger",
            })
        result["sa_seed_allocations"].append({
            "stagnation": int(match.group(1)),
            "diversity_level": int(match.group(2)),
            "groups": groups,
        })

    initial_temperatures: list[float] = []
    sa_summary_pattern = re.compile(
        r"downhill_attempted=(\d+), downhill_accepted=(\d+), .*?"
        r"downhill_acceptance=([0-9.eE+-]+)%.*?"
        r"initial_calibrated_T=([0-9.eE+-]+).*?reheats=(\d+)",
        flags=re.I,
    )
    for match in sa_summary_pattern.finditer(text):
        attempted = int(match.group(1))
        accepted = int(match.group(2))
        result["sa_group_summaries"] += 1
        result["sa_downhill_attempted"] += attempted
        result["sa_downhill_accepted"] += accepted
        initial_temperatures.append(float(match.group(4)))
        result["sa_reheats"] += int(match.group(5))
    if result["sa_downhill_attempted"]:
        result["sa_downhill_acceptance"] = (
            result["sa_downhill_accepted"] / result["sa_downhill_attempted"]
        )
    if initial_temperatures:
        result["sa_initial_temperature_min"] = min(initial_temperatures)
        result["sa_initial_temperature_max"] = max(initial_temperatures)

    role_stats: dict[str, dict[str, int]] = {}
    progress_pattern = re.compile(
        r"temperature_role=(cold|warm|hot), "
        r"(?:removal_mode=(sampled-explorer|exact-best), )?"
        r"best_score=(-?\d+), current_score=(-?\d+)", flags=re.I)
    removal_modes: dict[str, int] = {}
    for match in progress_pattern.finditer(text):
        role = match.group(1).lower()
        removal_mode = (match.group(2) or "legacy-unspecified").lower()
        best_score = int(match.group(3))
        current_score = int(match.group(4))
        removal_modes[removal_mode] = removal_modes.get(removal_mode, 0) + 1
        stats = role_stats.setdefault(role, {"samples": 0, "current_below_best": 0})
        stats["samples"] += 1
        result["sa_current_best_divergence_samples"] += 1
        if current_score < best_score:
            stats["current_below_best"] += 1
            result["sa_current_below_best_samples"] += 1
    result["sa_temperature_roles"] = role_stats
    result["sa_removal_modes"] = removal_modes

    repair_pattern = re.compile(
        r"incremental summary:.*?repair=(\d+)/(\d+) triggers=(\d+) full_scans=(\d+) "
        r"eval=(\d+) cache_recompute=(\d+) cache_rebuilds=(\d+) cache_refreshes=(\d+) "
        r"cached_pruned=(\d+) repair_blocks=(\d+)/(\d+) "
        r"partial_scans=(\d+) partial_salvaged=(\d+) admission_skips=(\d+) "
        r"repair_time=([0-9.eE+-]+) max_scan=([0-9.eE+-]+) "
        r"predicted_scan=([0-9.eE+-]+) max_grace=([0-9.eE+-]+)",
        flags=re.I,
    )
    for match in repair_pattern.finditer(text):
        result["repair_rounds_attempted"] += int(match.group(2))
        result["repair_full_scans"] += int(match.group(4))
        result["repair_evaluated_candidates"] += int(match.group(5))
        result["repair_cache_recomputed"] += int(match.group(6))
        result["repair_cached_pruned"] += int(match.group(9))
        result["repair_partial_scans"] += int(match.group(12))
        result["repair_partial_salvaged"] += int(match.group(13))
        result["repair_admission_skips"] += int(match.group(14))
        result["repair_scan_seconds"] += float(match.group(15))
        result["repair_max_scan_seconds"] = max(
            result["repair_max_scan_seconds"], float(match.group(16)))
        result["repair_max_predicted_scan_seconds"] = max(
            result["repair_max_predicted_scan_seconds"], float(match.group(17)))
        result["repair_max_grace_seconds"] = max(
            result["repair_max_grace_seconds"], float(match.group(18)))

    rotating_repair_pattern = re.compile(
        r"incremental summary:.*?rotating_partial=(\d+) "
        r"rotating_blocks=(\d+) rotating_elite=(\d+) "
        r"rotating_cursor=(\d+)->(\d+)",
        flags=re.I,
    )
    for match in rotating_repair_pattern.finditer(text):
        result["repair_rotating_partial_scans"] += int(match.group(1))
        result["repair_rotating_blocks_considered"] += int(match.group(2))
        result["repair_rotating_elite_prefix_blocks"] = max(
            result["repair_rotating_elite_prefix_blocks"], int(match.group(3)))
        result["repair_rotating_last_start_rank"] = int(match.group(4))
        result["repair_rotating_next_rank"] = int(match.group(5))

    result["ruin_calls"] = len(re.findall(r"Ruin/recreate phase", text))
    result["ruin_improvements"] = len(re.findall(r"Move \d+ ruin_recreate", text))
    destroyed: list[int] = []
    levels: list[int] = []
    for match in re.finditer(
        r"Ruin/recreate attempt\s+\d+/\d+\s+\[[^,]+, stagnation_level=(\d+)\]: destroyed=(\d+)/(\d+)",
        text, flags=re.I,
    ):
        result["ruin_attempts"] += 1
        levels.append(int(match.group(1)))
        destroyed.append(int(match.group(2)))
    mode_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"attempts": 0, "incumbent_improvements": 0})
    for match in re.finditer(
        r"Ruin/recreate attempt\s+\d+/\d+\s+\[([^,]+),.*?improves_incumbent=(yes|no)",
        text, flags=re.I,
    ):
        mode = match.group(1).strip()
        mode_stats[mode]["attempts"] += 1
        if match.group(2).lower() == "yes":
            mode_stats[mode]["incumbent_improvements"] += 1
    result["ruin_modes"] = dict(sorted(mode_stats.items()))
    if destroyed:
        result["ruin_destroyed_min"] = min(destroyed)
        result["ruin_destroyed_max"] = max(destroyed)
    if levels:
        result["ruin_stagnation_level_max"] = max(levels)

    crossover_matches = list(re.finditer(r"Elite crossover:.*?accepted=(yes|no)", text, flags=re.I))
    result["crossover_calls"] = len(crossover_matches)
    result["crossover_improvements"] = sum(
        1 for match in crossover_matches if match.group(1).lower() == "yes"
    )

    exploration_drops: list[int] = []
    exploration_distances: list[int] = []
    exploration_pattern = re.compile(
        r"Exploration seed queued from (.*?): score=(-?\d+)/\d+ "
        r"\(drop=(\d+)/(\d+)\), distance=(\d+), pending=(\d+)/(\d+)",
        flags=re.I,
    )
    for match in exploration_pattern.finditer(text):
        event = {
            "source": match.group(1).strip(),
            "score": int(match.group(2)),
            "drop": int(match.group(3)),
            "allowed_drop": int(match.group(4)),
            "distance": int(match.group(5)),
            "pending": int(match.group(6)),
            "limit": int(match.group(7)),
        }
        result["exploration_seed_events"].append(event)
        exploration_drops.append(event["drop"])
        exploration_distances.append(event["distance"])
    summary_matches = re.findall(
        r"transient-exploration-seeds: queued=(\d+), "
        r"consumed_by_SA=(\d+), pending=(\d+)",
        text, flags=re.I,
    )
    if summary_matches:
        queued, consumed, pending = summary_matches[-1]
        result["exploration_seeds_queued"] = int(queued)
        result["exploration_seeds_consumed"] = int(consumed)
        result["exploration_seeds_pending"] = int(pending)
    else:
        result["exploration_seeds_queued"] = len(result["exploration_seed_events"])
    if exploration_drops:
        result["exploration_score_drop_max_observed"] = max(exploration_drops)
    if exploration_distances:
        result["exploration_distance_min"] = min(exploration_distances)
        result["exploration_distance_max"] = max(exploration_distances)
    result["one_swap_moves"] = len(
        re.findall(r"Move \d+ .*1-swap|Move \d+ swap", text, flags=re.I)
    )
    cycle_stagnation = [int(x) for x in re.findall(
        r"Portfolio cycle \d+:.*?stagnation=(\d+)", text, flags=re.I
    )]
    result["portfolio_cycles"] = len(cycle_stagnation)
    if cycle_stagnation:
        result["portfolio_stagnation_max"] = max(cycle_stagnation)

    checkpoint_matches = re.findall(
        r"Atomic incumbent checkpoint\s+(\d+): score=(\d+)/",
        text, flags=re.I,
    )
    result["checkpoints"] = len(checkpoint_matches)
    if checkpoint_matches:
        result["last_checkpoint_sequence"] = int(checkpoint_matches[-1][0])
        result["last_checkpoint_score"] = int(checkpoint_matches[-1][1])

    stats_pattern = re.compile(
        r"^\s*(exact-1-swap|grouped-SA|ruin-recreate|elite-crossover|experimental-CP-SAT):\s*"
        r"attempts=(\d+), improvements=(\d+), time=([^,]+), "
        r"score_gain=(-?\d+), near_gain=([-+0-9.eE]+), "
        r"recent_reward=([-+0-9.eE]+)",
        flags=re.M,
    )
    for match in stats_pattern.finditer(text):
        result["operator_stats"][match.group(1)] = {
            "attempts": int(match.group(2)),
            "improvements": int(match.group(3)),
            "time": match.group(4).strip(),
            "score_gain": int(match.group(5)),
            "near_gain": safe_float(match.group(6)),
            "recent_reward": safe_float(match.group(7)),
        }

    result["exact_canonical_verifier_seen"] = (
        "canonical source-edge provenance is direct" in text
        and "exact arrangement ready" in text
    )
    result["legacy_wiggle_seen"] = bool(re.search(
        r"continuous polishing|contiguous wiggle|incident-edge slide",
        text, flags=re.I,
    )) and "continuous movement disabled" not in text.lower()

    wiggle_init = list(re.finditer(
        r"\[wiggle\]\[init\] discrete reconstruction .*?"
        r"movable_edge_guards=(\d+), frozen_vertices=(\d+), .*?"
        r"qualification_set_match=(yes|NO)",
        text, flags=re.I,
    ))
    if wiggle_init:
        match = wiggle_init[-1]
        result["wiggle_seen"] = True
        result["wiggle_movable_edge_guards"] = int(match.group(1))
        result["wiggle_frozen_vertices"] = int(match.group(2))
        result["wiggle_baseline_match"] = match.group(3).lower() == "yes"

    wiggle_complete = list(re.finditer(
        r"\[wiggle\] complete: rounds=(\d+), movable_edge_guards=(\d+), "
        r"frozen_vertices=(\d+), trials=(\d+), completed_rows=(\d+), "
        r"accepted=(\d+), momentum_continuations=(\d+), "
        r"score_improvements=(\d+), score=(\d+)->(\d+), "
        r"near_progress=([-+0-9.eE]+)->([-+0-9.eE]+)",
        text, flags=re.I,
    ))
    if wiggle_complete:
        match = wiggle_complete[-1]
        result["wiggle_seen"] = True
        result["wiggle_movable_edge_guards"] = int(match.group(2))
        result["wiggle_frozen_vertices"] = int(match.group(3))
        result["wiggle_trials"] = int(match.group(4))
        result["wiggle_completed_rows"] = int(match.group(5))
        result["wiggle_accepted"] = int(match.group(6))
        result["wiggle_momentum_continuations"] = int(match.group(7))
        result["wiggle_score_improvements"] = int(match.group(8))
        result["wiggle_score_start"] = int(match.group(9))
        result["wiggle_score_end"] = int(match.group(10))
        result["wiggle_near_progress_start"] = safe_float(match.group(11))
        result["wiggle_near_progress_end"] = safe_float(match.group(12))

    errors = []
    for line in text.splitlines():
        if re.search(r"\bERROR\b|SOLUTION IS INFEASIBLE|std::bad_alloc|OUT_OF_MEMORY", line, re.I):
            errors.append(line.strip())
    result["errors"] = errors[-30:]
    return result


def recommendations(report: dict[str, Any]) -> list[str]:
    recs: list[str] = []
    verification = report["verification"]
    timeline = report["timeline"]
    log = report["log"]
    if verification.get("feasible") not in {"yes", None}:
        recs.append("Correctness failure: inspect verifier violations before tuning search.")
    if verification.get("preverify_score_delta") not in {None, 0}:
        recs.append("Cached and canonical scores diverged; prioritize geometry/cache consistency.")
    if log.get("sa_phases", 0) and log.get("sa_improvements", 0) == 0:
        recs.append("SA produced no accepted incumbent: compare calibrated losses, proposal quality, and seed diversity.")
    downhill_rate = log.get("sa_downhill_acceptance")
    if log.get("sa_downhill_attempted", 0) and downhill_rate is not None:
        if downhill_rate < 0.01:
            recs.append("SA downhill acceptance stayed below 1%; inspect calibration scale or increase reheating frequency.")
        elif downhill_rate > 0.35:
            recs.append("SA accepted over 35% of downhill proposals overall; cool faster or improve proposal targeting.")
    if log.get("repair_partial_scans", 0):
        salvage = log.get("repair_partial_salvaged", 0)
        partial = log.get("repair_partial_scans", 0)
        recs.append(
            f"Live repair hit {partial} partial scan(s) and salvaged {salvage}; compare predicted-vs-max scan time before changing grace/admission."
        )
    if log.get("repair_rotating_partial_scans", 0):
        recs.append(
            "Deadline-limited repair used strongest-prefix + rotating ranked-block coverage; inspect rotating block count/cursor together with salvage rate before changing scan policy."
        )
    if log.get("repair_admission_skips", 0):
        recs.append(
            "Repair admission skipped deeper scans; this is expected protection on large universes—tune only if full-scan completion is also low."
        )
    if log.get("ruin_calls", 0) and log.get("ruin_improvements", 0) == 0:
        recs.append("Ruin/recreate was cold: compare per-mode evidence and refill pool quality before changing the nine-mode mix.")
    if log.get("edge_pool_accepted") and log.get("expanded_universe_size"):
        before_counts = log.get("candidate_counts") or []
        if len(before_counts) >= 2 and max(before_counts) <= min(before_counts):
            recs.append("The accepted edge pool did not visibly expand candidate count; inspect foundry generation/deduplication.")
    if log.get("crossover_calls", 0) >= 3 and log.get("crossover_improvements", 0) == 0:
        recs.append("Crossover made no improvements; reduce its share or use threshold-basin/path-relinking crossover.")
    if timeline.get("first_k_seconds") is not None and timeline.get("elapsed_seconds"):
        ratio = timeline["first_k_seconds"] / max(timeline["elapsed_seconds"], 1e-9)
        if ratio > 0.35:
            recs.append("Construction consumed over 35% of optimizer time; reduce growth optional work or use stronger warm starts.")
        elif ratio < 0.10:
            recs.append("Construction is cheap; consider multiple diverse greedy starts before fixed-k search.")
    nearest = verification.get("nearest_unqualified") or []
    if len(nearest) >= 10 and nearest[9]["deficit_m"] < 0.10:
        recs.append("Many polygons are within 10 cm of qualification; increase deficit-focused proposals and targeted ruin.")
    if not recs:
        recs.append("No obvious failure signature; compare verified gain-per-hour across seeds and operator allocations.")
    return recs


def format_seconds(value: Any) -> str:
    seconds = safe_float(value)
    if seconds is None:
        return "n/a"
    hours, rem = divmod(max(0, int(round(seconds))), 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {sec:02d}s"
    if minutes:
        return f"{minutes}m {sec:02d}s"
    return f"{sec}s"


def render_markdown(report: dict[str, Any]) -> str:
    meta = report["metadata"]
    ver = report["verification"]
    tl = report["timeline"]
    log = report["log"]
    resources = report["resources"]
    lines = [
        "# RINGARC28 portfolio-annealer feedback report",
        "",
        f"- Status: **{meta.get('status', 'unknown')}**; exit code `{meta.get('exit_code')}`",
        f"- Cell: `k={meta.get('k')}`, `tau={meta.get('tau')}`, replica `{meta.get('replica')}` / `{meta.get('profile')}`, SA seed `{meta.get('sa_seed')}`, ruin seed `{meta.get('ruin_seed')}`",
        f"- Revision: `{meta.get('revision')}`; job `{meta.get('job_id')}` on `{meta.get('host')}`",
        f"- Search profile: temp `{meta.get('sa_start_temperature')}`→`{meta.get('sa_end_temperature')}`; exact/SA/ruin/crossover `{meta.get('portfolio_one_swap_fraction')}`/`{meta.get('portfolio_sa_fraction')}`/`{meta.get('portfolio_ruin_fraction')}`/`{meta.get('portfolio_crossover_fraction')}`; active seeds `{meta.get('active_sa_seeds')}`, passive elites `{meta.get('elite_pool_size')}`",
        f"- Phase budget: vertex pool cap `{format_seconds(meta.get('edge_vertex_seconds'))}`; protected verification reserve `{format_seconds(meta.get('verify_reserve_seconds'))}`",
        f"- Candidate universe: {'expanded exact edge pool' if log.get('edge_pool_accepted') else 'exact vertex pool'}; counts `{log.get('candidate_counts') or 'unknown'}`, final `{log.get('candidate_count') or 'unknown'}`, intervals `{log.get('coverage_intervals') or 'unknown'}`",
        f"- Edge handoff: accepted=`{log.get('edge_pool_accepted')}`, expanded size=`{log.get('expanded_universe_size')}`, remapped elites=`{log.get('remapped_restart_seeds')}`; all covered arcs recomputed=`{bool(log.get('edge_pool_accepted') and log.get('expanded_universe_size'))}`",
        f"- **Final optimizer score: `{report.get('result_summary', {}).get('solver_final_score')}`**; best observed `{report.get('result_summary', {}).get('solver_best_score')}`; score source `{report.get('result_summary', {}).get('score_source')}`",
        (f"- Canonical verification: feasible=`{ver.get('feasible')}`, score=`{ver.get('real_score')}`, cached-before-verify=`{ver.get('preverify_cached_score')}`, delta=`{ver.get('preverify_score_delta')}`"
         if ver.get('present') else
         "- Canonical verification: **not run in this job** (`GRID_VERIFY=0`/no verification summary); persisted submission still requires external/website verification."),
        f"- Time to k: {format_seconds(tl.get('first_k_seconds'))}; timeline duration: {format_seconds(tl.get('elapsed_seconds'))}; verifier: {format_seconds(ver.get('runtime_seconds'))}",
        "",
        "## Operator evidence",
        "",
        f"- Accepted timeline moves: `{tl.get('accepted_moves')}`; by phase: `{json.dumps(tl.get('accepted_by_phase', {}), sort_keys=True)}`",
        f"- Score gain by phase: `{json.dumps(tl.get('score_gain_by_phase', {}), sort_keys=True)}`",
        f"- SA calls/improvements: `{log.get('sa_phases')}/{log.get('sa_improvements')}`; group summaries `{log.get('sa_group_summaries')}`; downhill `{log.get('sa_downhill_accepted')}/{log.get('sa_downhill_attempted')}` = `{(100.0 * log['sa_downhill_acceptance'] if log.get('sa_downhill_acceptance') is not None else 'n/a')}`%; reheats `{log.get('sa_reheats')}`",
        f"- SA phase horizons: `{json.dumps(log.get('sa_phase_seconds_by_label', {}), sort_keys=True)}`; post-k assigned seconds `{log.get('post_k_sa_assigned_seconds')}`",
        f"- Live repair: rounds `{log.get('repair_rounds_attempted')}`, full scans `{log.get('repair_full_scans')}`, partial scans `{log.get('repair_partial_scans')}`, partial swaps salvaged `{log.get('repair_partial_salvaged')}`, admission skips `{log.get('repair_admission_skips')}`; rotating-partial scans `{log.get('repair_rotating_partial_scans')}`, rotating remainder blocks `{log.get('repair_rotating_blocks_considered')}`, elite prefix `{log.get('repair_rotating_elite_prefix_blocks')}`, last cursor `{log.get('repair_rotating_last_start_rank')}->{log.get('repair_rotating_next_rank')}`; exact eval `{log.get('repair_evaluated_candidates')}`, cached-pruned `{log.get('repair_cached_pruned')}`, cache recompute `{log.get('repair_cache_recomputed')}`; repair time `{format_seconds(log.get('repair_scan_seconds'))}`, max scan `{format_seconds(log.get('repair_max_scan_seconds'))}`, max predicted `{format_seconds(log.get('repair_max_predicted_scan_seconds'))}`, max grace `{format_seconds(log.get('repair_max_grace_seconds'))}`",
        f"- SA current/best telemetry: divergence samples `{log.get('sa_current_best_divergence_samples')}`, current-below-best `{log.get('sa_current_below_best_samples')}`; roles `{json.dumps(log.get('sa_temperature_roles', {}), sort_keys=True)}`; removal modes `{json.dumps(log.get('sa_removal_modes', {}), sort_keys=True)}`",
        f"- SA active-seed allocations: `{json.dumps(log.get('sa_seed_allocations', []), sort_keys=True)}`",
        f"- Ruin calls/improvements: `{log.get('ruin_calls')}/{log.get('ruin_improvements')}`; attempts `{log.get('ruin_attempts')}`, destroyed `{log.get('ruin_destroyed_min')}`–`{log.get('ruin_destroyed_max')}`, max stagnation level `{log.get('ruin_stagnation_level_max')}`; per-mode `{json.dumps(log.get('ruin_modes', {}), sort_keys=True)}`",
        f"- Crossover calls/improvements: `{log.get('crossover_calls')}/{log.get('crossover_improvements')}`; portfolio cycles `{log.get('portfolio_cycles')}`, max stagnation `{log.get('portfolio_stagnation_max')}`",
        f"- Transient excursion seeds: queued `{log.get('exploration_seeds_queued')}`, consumed by SA `{log.get('exploration_seeds_consumed')}`, pending `{log.get('exploration_seeds_pending')}`; max accepted score drop `{log.get('exploration_score_drop_max_observed')}`, distance range `{log.get('exploration_distance_min')}`–`{log.get('exploration_distance_max')}`",
        f"- Exact 1-swap move lines: `{log.get('one_swap_moves')}`; atomic checkpoints `{log.get('checkpoints')}`, last sequence/score `{log.get('last_checkpoint_sequence')}/{log.get('last_checkpoint_score')}`",
        f"- End-of-run operator statistics: `{json.dumps(log.get('operator_stats', {}), sort_keys=True)}`",
        f"- Edge-only threshold wiggle: seen=`{log.get('wiggle_seen')}`, baseline_match=`{log.get('wiggle_baseline_match')}`, movable edges/frozen vertices `{log.get('wiggle_movable_edge_guards')}/{log.get('wiggle_frozen_vertices')}`, trials/completed `{log.get('wiggle_trials')}/{log.get('wiggle_completed_rows')}`, accepted `{log.get('wiggle_accepted')}` (momentum `{log.get('wiggle_momentum_continuations')}`), score improvements `{log.get('wiggle_score_improvements')}`, score `{log.get('wiggle_score_start')}`→`{log.get('wiggle_score_end')}`, near-progress `{log.get('wiggle_near_progress_start')}`→`{log.get('wiggle_near_progress_end')}`",
        "",
        "## Threshold frontier",
        "",
        f"- Exact near-threshold count: `{ver.get('near_threshold_polygons')}`",
        f"- Margin bands: `{json.dumps(ver.get('threshold_margin_bands', {}), sort_keys=True)}`",
    ]
    nearest = ver.get("nearest_unqualified") or []
    if nearest:
        lines.append("- Closest unqualified polygons (metres deficit):")
        for item in nearest[:10]:
            lines.append(
                f"  - polygon `{item.get('polygon_id')}` / source `{item.get('source_fid')}`: "
                f"`{item.get('deficit_m'):.9g}` m"
            )
    lines.extend(["", "## Resource envelope", ""])
    for phase, usage in resources.items():
        lines.append(
            f"- {phase}: elapsed `{usage.get('elapsed_wall', 'n/a')}`, "
            f"max RSS `{usage.get('max_rss_gib', 'n/a')}` GiB, exit `{usage.get('exit_status', 'n/a')}`"
        )
    lines.extend(["", "## Suggested next experiments", ""])
    for recommendation in report["recommendations"]:
        lines.append(f"- {recommendation}")
    if log.get("errors"):
        lines.extend(["", "## Error tail", ""])
        for error in log["errors"]:
            lines.append(f"- `{error}`")
    lines.extend([
        "",
        "## Artifact pointers",
        "",
        f"- Run directory: `{meta.get('run_dir')}`",
        f"- Timeline: `{tl.get('path')}`",
        f"- Verification summary: `{ver.get('summary_path')}`",
        f"- Submission block: `{meta.get('submission_block')}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--revision", default="unknown")
    parser.add_argument("--job-id", default=os.environ.get("SLURM_JOB_ID", "unknown"))
    parser.add_argument("--k", type=int)
    parser.add_argument("--tau", type=float)
    parser.add_argument("--replica", type=int, default=0)
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--sa-start", type=float)
    parser.add_argument("--sa-end", type=float)
    parser.add_argument("--one-swap-fraction", type=float)
    parser.add_argument("--sa-fraction", type=float)
    parser.add_argument("--ruin-fraction", type=float)
    parser.add_argument("--crossover-fraction", type=float)
    parser.add_argument("--active-sa-seeds", type=int)
    parser.add_argument("--elite-pool-size", type=int)
    parser.add_argument("--edge-vertex-seconds", type=int)
    parser.add_argument("--edge-search-reserve-seconds", type=int)
    parser.add_argument("--verify-reserve-seconds", type=int)
    parser.add_argument("--sa-seed", type=int)
    parser.add_argument("--ruin-seed", type=int)
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    timeline = parse_timeline(run_dir / "optimization_timeline.csv")
    verification = parse_verification(run_dir)
    log = parse_log(args.log)
    resources = {
        "preprocess": parse_time_usage(run_dir / "resource_preprocess.txt"),
        "optimize": parse_time_usage(run_dir / "resource_optimize.txt"),
        "edge_optimize": parse_time_usage(run_dir / "resource_edge_optimize.txt"),
        "wiggle": parse_time_usage(run_dir / "resource_wiggle.txt"),
        "verify": parse_time_usage(run_dir / "resource_verify.txt"),
    }
    submission_present = (run_dir / "submission_block.txt").is_file()
    independently_verified = bool(
        verification.get("feasible") == "yes"
        and verification.get("gate_marker_present")
    )
    # RINGARC31 separates solver completion from optional heavy verification.
    # A competition-style GRID_VERIFY=0 run is healthy when search exits zero
    # and has atomically persisted a submission block; it is merely unverified.
    if args.exit_code == 0 and independently_verified:
        status = "complete_verified"
    elif args.exit_code == 0 and submission_present:
        status = "complete_unverified"
    else:
        status = "partial_or_failed"
    solver_final_score = timeline.get("final_score")
    if not isinstance(solver_final_score, int):
        solver_final_score = log.get("last_checkpoint_score")
    solver_best_score = timeline.get("best_score")
    if not isinstance(solver_best_score, int):
        solver_best_score = solver_final_score
    canonical_score = verification.get("real_score")
    if isinstance(canonical_score, int):
        score_source = "canonical_verification"
        reported_score = canonical_score
    else:
        score_source = "optimization_timeline" if isinstance(timeline.get("final_score"), int) else "atomic_checkpoint"
        reported_score = solver_final_score

    report: dict[str, Any] = {
        "schema": "ringarc28.portfolio-annealer-feedback.v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "status": status,
            "search_completed": status in {"complete_verified", "complete_unverified"},
            "independently_verified": independently_verified,
            "submission_block_present": submission_present,
            "exit_code": args.exit_code,
            "revision": args.revision,
            "job_id": args.job_id,
            "host": socket.gethostname(),
            "k": args.k,
            "tau": args.tau,
            "replica": args.replica,
            "profile": args.profile,
            "sa_start_temperature": args.sa_start,
            "sa_end_temperature": args.sa_end,
            "portfolio_one_swap_fraction": args.one_swap_fraction,
            "portfolio_sa_fraction": args.sa_fraction,
            "portfolio_ruin_fraction": args.ruin_fraction,
            "portfolio_crossover_fraction": args.crossover_fraction,
            "active_sa_seeds": args.active_sa_seeds,
            "elite_pool_size": args.elite_pool_size,
            "edge_vertex_seconds": args.edge_vertex_seconds,
            "edge_search_reserve_seconds": args.edge_search_reserve_seconds,
            "verify_reserve_seconds": args.verify_reserve_seconds,
            "sa_seed": args.sa_seed,
            "ruin_seed": args.ruin_seed,
            "run_dir": str(run_dir),
            "submission_block": str(run_dir / "submission_block.txt"),
        },
        "timeline": timeline,
        "verification": verification,
        "log": log,
        "resources": resources,
        "result_summary": {
            "reported_score": reported_score,
            "score_source": score_source,
            "solver_final_score": solver_final_score,
            "solver_best_score": solver_best_score,
            "canonical_score": canonical_score,
            "independently_verified": independently_verified,
        },
    }
    report["recommendations"] = recommendations(report)

    json_path = run_dir / "conclusion_report.json"
    md_path = run_dir / "conclusion_report.md"
    markdown = render_markdown(report)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    md_path.write_text(markdown)

    print("===== BEGIN RINGARC28 PORTFOLIO ANNEALER FEEDBACK PACKET =====")
    print(markdown, end="")
    print("===== END RINGARC28 PORTFOLIO ANNEALER FEEDBACK PACKET =====")
    print(f"Conclusion JSON: {json_path}")
    print(f"Conclusion Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
