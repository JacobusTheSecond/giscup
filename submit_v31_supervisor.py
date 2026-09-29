#!/usr/bin/env python3
"""RINGARC31 arbitrary-parameter nine-cell plus edge-foundry supervisor.

The three runtime k values and three runtime tau values form nine scoring cells.
Cells search the exact vertex universe first and consider the authenticated edge
foundry only in the scheduled late slice. Heavy verification is deliberately
outside the competition search horizon by default.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from tools import campaign_utils as base

EXPECTED_REVISION = "RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE"
RUNNING_STATES = {"RUNNING", "CONFIGURING", "COMPLETING"}
TERMINAL_SUCCESS_STATES = {"COMPLETED"}
TERMINAL_NO_RESTART_STATES = {"TIMEOUT", "DEADLINE"}
TERMINAL_FAILURE_STATES = {
    "BOOT_FAIL", "CANCELLED", "FAILED", "NODE_FAIL",
    "OUT_OF_MEMORY", "PREEMPTED", "REVOKED",
}
BAD_NODE_FLAGS = {
    "DOWN", "DRAIN", "DRAINING", "FAIL", "FAILING", "MAINT",
    "NOT_RESPONDING", "POWER_DOWN", "POWERING_DOWN", "REBOOT",
    "RESERVED", "UNKNOWN",
}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def strict_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def run_capture(command: list[str], timeout: int | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, (stdout + stderr + "\ncommand timed out").strip()
    return result.returncode, result.stdout.strip()


def parse_kv_line(line: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]*)", line)
    }


def state_base(value: str) -> str:
    return re.split(r"[+~#$* ]", (value or "UNKNOWN").upper(), maxsplit=1)[0]


def short_node(value: str) -> str:
    value = (value or "").strip()
    if value in {"", "(null)", "None", "N/A"}:
        return ""
    return value.split(".", 1)[0]


def round_down(value: int, step: int) -> int:
    if step <= 1:
        return value
    return (value // step) * step


def active_user_jobs() -> list[str]:
    code, text = run_capture([
        "squeue", "-h", "-u", os.environ.get("USER", ""),
        "-o", "%i|%j|%T|%R",
    ], timeout=20)
    if code != 0:
        raise RuntimeError("squeue failed while enforcing the user job cap: {}".format(text))
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()]


def wait_for_user_job_slot(context: str) -> None:
    """Never let RINGARC31 push the user's Slurm job count above the hard cap."""
    limit = env_int("GRID_USER_JOB_LIMIT", 10, 1)
    announced = False
    while True:
        try:
            jobs = active_user_jobs()
        except RuntimeError as exc:
            if not announced:
                print("Slurm job cap guard could not read squeue; refusing to submit until it recovers: {}".format(exc), flush=True)
                announced = True
            time.sleep(env_int("GRID_JOB_SLOT_POLL_SECONDS", 20, 10))
            continue
        if len(jobs) < limit:
            return
        if not announced:
            print(
                "Slurm job cap guard: {}/{} user jobs active while trying to submit {}; waiting for a slot.".format(
                    len(jobs), limit, context
                ),
                flush=True,
            )
            announced = True
        time.sleep(env_int("GRID_JOB_SLOT_POLL_SECONDS", 20, 10))


def enter_steady_state_niceness() -> None:
    """Yield gateway CPU after admission without slowing the startup wave logic."""
    delta = env_int("GRID_STEADY_NICE_DELTA", 10, 0)
    if delta <= 0:
        return
    try:
        new_value = os.nice(delta)
        print(
            "Startup complete; supervisor entering low-impact steady state "
            "(nice={}).".format(new_value),
            flush=True,
        )
    except OSError as exc:
        print(
            "WARNING: could not lower supervisor priority after startup: {}".format(exc),
            flush=True,
        )


@dataclass(frozen=True)
class Task:
    order: int
    k: int
    tau: float
    tag: str
    min_cpus: int
    min_mem_mb: int
    max_cpus: int
    max_mem_mb: int

    @property
    def difficulty(self) -> tuple[int, float]:
        return self.k, self.tau


@dataclass(frozen=True)
class NodeFragment:
    name: str
    state: str
    cpu_total: int
    cpu_alloc: int
    cpu_free: int
    mem_total_mb: int
    mem_alloc_mb: int
    mem_free_mb: int
    safe_mem_mb: int


@dataclass
class Envelope:
    task: Task
    basis_node: str
    cpus: int
    mem_mb: int
    attempt: int


@dataclass
class CellRecord:
    task: Task
    job_id: str = ""
    state: str = "WAITING"
    reason: str = ""
    node: str = ""
    cpus: int = 0
    mem_mb: int = 0
    attempt: int = 0
    basis_node: str = ""
    startup_ok: bool = False
    submitted_at: float = 0.0
    running_since: float = 0.0


def parse_int_csv(name: str, default: str) -> list[int]:
    raw = os.environ.get(name, default)
    try:
        values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a comma-separated integer list") from exc
    if len(values) != 3 or len(set(values)) != 3 or any(value <= 0 for value in values):
        raise RuntimeError(f"{name} must contain exactly three distinct positive values")
    return values


def parse_float_csv(name: str, default: str) -> list[float]:
    raw = os.environ.get(name, default)
    try:
        values = [float(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a comma-separated numeric list") from exc
    if len(values) != 3 or len({struct.pack(">d", value) for value in values}) != 3:
        raise RuntimeError(f"{name} must contain exactly three distinct binary64 values")
    if any(not (0.0 <= value <= 1.0) or not math.isfinite(value) for value in values):
        raise RuntimeError(f"{name} values must be finite and in [0,1]")
    return values


def tau_token(tau: float) -> str:
    # Collision-proof token for the exact runtime binary64 value.  Human-readable
    # tau remains in jobs.tsv/logs; filenames must never alias two close official
    # parameters merely because they share six decimal places.
    return struct.pack(">d", tau).hex()


def _log_interp(k: int, anchors: list[tuple[int, float]]) -> float:
    k = max(1, k)
    anchors = sorted(anchors)
    if k <= anchors[0][0]:
        return anchors[0][1]
    if k >= anchors[-1][0]:
        return anchors[-1][1]
    lk = math.log(k)
    for (ka, va), (kb, vb) in zip(anchors, anchors[1:]):
        if ka <= k <= kb:
            t = (lk - math.log(ka)) / (math.log(kb) - math.log(ka))
            return va + t * (vb - va)
    return anchors[-1][1]


def resource_policy(k: int) -> dict[str, int]:
    # RINGARC31.0.3: CPU is the performance resource for workers.  The previous
    # 24h campaign kept every worker below ~36.4 GiB peak RSS, so a fixed 128 GiB
    # request leaves a large safety margin without excluding high-core nodes.
    # Preserve the proven k-scaled CPU hierarchy so large-k waves still get first
    # claim on the fattest CPU fragments.
    min_cpu = round(_log_interp(k, [(50, 16), (500, 28), (5000, 32), (10000, 40)]))
    max_cpu = round(_log_interp(k, [(50, 64), (500, 96), (5000, 112), (10000, 128)]))
    worker_mem_gb = env_int("GRID_WORKER_MEM_GB", 128, 64)
    return {
        "min_cpu": max(8, min_cpu),
        "min_mem": worker_mem_gb * 1024,
        "max_cpu": max(max(8, min_cpu), min(128, max_cpu)),
        "max_mem": worker_mem_gb * 1024,
    }


def task_list() -> list[Task]:
    mem_per_cpu = env_int("GRID_MEM_PER_BILLING_CPU_MB", 8192, 1024)
    k_values = sorted(parse_int_csv("GRID_K_VALUES", "500,5000,10000"), reverse=True)
    tau_values = sorted(parse_float_csv("GRID_TAU_VALUES", "0.25,0.50,0.75"), reverse=True)
    tasks: list[Task] = []
    order = 0
    for k in k_values:
        policy = resource_policy(k)
        min_cpus = max(
            policy["min_cpu"],
            int(math.ceil(policy["min_mem"] / float(mem_per_cpu))),
        )
        max_cpus = max(min_cpus, policy["max_cpu"])
        max_mem = min(policy["max_mem"], max_cpus * mem_per_cpu)
        for tau in tau_values:
            tag = f"t{tau_token(tau)}_k{k}_r00"
            tasks.append(Task(
                order=order,
                k=k,
                tau=tau,
                tag=tag,
                min_cpus=min_cpus,
                min_mem_mb=policy["min_mem"],
                max_cpus=max_cpus,
                max_mem_mb=max_mem,
            ))
            order += 1
    return tasks


def discover_nodes(partition: str, excluded: set[str]) -> list[NodeFragment]:
    code, text = run_capture(["scontrol", "show", "nodes", "-o"], timeout=30)
    if code != 0:
        raise RuntimeError("scontrol show nodes failed: {}".format(text))

    mem_per_cpu = env_int("GRID_MEM_PER_BILLING_CPU_MB", 8192, 1024)
    cpu_reserve = env_int("GRID_NODE_CPU_RESERVE", 0, 0)
    mem_reserve_mb = env_int("GRID_NODE_MEM_RESERVE_GB", 8, 0) * 1024
    fragments: list[NodeFragment] = []

    for line in text.splitlines():
        fields = parse_kv_line(line)
        name = short_node(fields.get("NodeName", ""))
        if not name or name in excluded:
            continue
        partitions = set(filter(None, fields.get("Partitions", "").split(",")))
        if partition not in partitions:
            continue
        raw_state = fields.get("State", "UNKNOWN").upper()
        if any(flag in raw_state for flag in BAD_NODE_FLAGS):
            continue
        try:
            cpu_total = int(fields.get("CPUEfctv", fields.get("CPUTot", "0")) or 0)
            cpu_alloc = int(fields.get("CPUAlloc", "0") or 0)
            mem_total_mb = int(fields.get("RealMemory", "0") or 0)
            mem_alloc_mb = int(fields.get("AllocMem", "0") or 0)
        except ValueError:
            continue

        cpu_free = max(0, cpu_total - cpu_alloc - cpu_reserve)
        mem_free_mb = max(0, mem_total_mb - mem_alloc_mb - mem_reserve_mb)
        safe_mem_mb = min(mem_free_mb, cpu_free * mem_per_cpu)
        if cpu_free <= 0 or safe_mem_mb <= 0:
            continue
        fragments.append(NodeFragment(
            name=name,
            state=state_base(raw_state),
            cpu_total=cpu_total,
            cpu_alloc=cpu_alloc,
            cpu_free=cpu_free,
            mem_total_mb=mem_total_mb,
            mem_alloc_mb=mem_alloc_mb,
            mem_free_mb=mem_free_mb,
            safe_mem_mb=safe_mem_mb,
        ))

    # CPU is the primary worker resource.  Safe memory breaks ties and already
    # incorporates the Hendrix 8-GiB-per-CPU scheduler constraint.
    fragments.sort(key=lambda node: (
        -node.cpu_free,
        -node.safe_mem_mb,
        -node.cpu_total,
        node.name,
    ))
    return fragments


def choose_initial_envelope(
    task: Task,
    nodes: list[NodeFragment],
    cpu_ceiling: int | None = None,
    mem_ceiling_mb: int | None = None,
) -> Envelope | None:
    cpu_step = env_int("GRID_CPU_GRANULARITY", 4, 1)
    mem_step_mb = env_int("GRID_MEM_GRANULARITY_GB", 8, 1) * 1024
    mem_per_cpu = env_int("GRID_MEM_PER_BILLING_CPU_MB", 8192, 1024)

    task_cpu_cap = task.max_cpus if cpu_ceiling is None else min(task.max_cpus, cpu_ceiling)
    task_mem_cap = task.max_mem_mb if mem_ceiling_mb is None else min(task.max_mem_mb, mem_ceiling_mb)

    for node in nodes:
        cpus = round_down(min(task_cpu_cap, node.cpu_free), cpu_step)
        if cpus < task.min_cpus:
            continue
        mem_mb = round_down(min(
            task_mem_cap,
            node.safe_mem_mb,
            cpus * mem_per_cpu,
        ), mem_step_mb)
        if mem_mb < task.min_mem_mb:
            continue
        return Envelope(task, node.name, cpus, mem_mb, 0)
    return None


def lower_envelope(current: Envelope) -> Envelope:
    task = current.task
    cpu_step = env_int("GRID_CPU_STEP", 8, 1)
    cpu_percent = env_float("GRID_CPU_STEP_PERCENT", 8.0, 0.0)
    cpu_granularity = env_int("GRID_CPU_GRANULARITY", 4, 1)
    mem_step_mb = env_int("GRID_MEM_STEP_GB", 16, 1) * 1024
    mem_percent = env_float("GRID_MEM_STEP_PERCENT", 4.0, 0.0)
    mem_granularity_mb = env_int("GRID_MEM_GRANULARITY_GB", 8, 1) * 1024
    mem_per_cpu = env_int("GRID_MEM_PER_BILLING_CPU_MB", 8192, 1024)

    cpu_drop = max(cpu_step, int(math.ceil(current.cpus * cpu_percent / 100.0)))
    next_cpus = round_down(max(task.min_cpus, current.cpus - cpu_drop), cpu_granularity)
    next_cpus = max(task.min_cpus, next_cpus)

    mem_drop = max(mem_step_mb, int(math.ceil(current.mem_mb * mem_percent / 100.0)))
    next_mem = current.mem_mb - mem_drop
    next_mem = min(next_mem, next_cpus * mem_per_cpu, task.max_mem_mb)
    next_mem = round_down(max(task.min_mem_mb, next_mem), mem_granularity_mb)
    next_mem = max(task.min_mem_mb, next_mem)

    return Envelope(task, current.basis_node, next_cpus, next_mem, current.attempt + 1)


def envelope_at_floor(envelope: Envelope) -> bool:
    return envelope.cpus <= envelope.task.min_cpus and envelope.mem_mb <= envelope.task.min_mem_mb


def make_export(values: dict[str, str]) -> str:
    return "ALL," + ",".join("{}={}".format(k, v) for k, v in sorted(values.items()))


def make_cell_export(task: Task, common: dict[str, str], cpus: int) -> dict[str, str]:
    seed_base = env_int("GRID_SEED_BASE", 275000)
    values = dict(common)
    scale = max(task.k / 500.0, 0.10)
    construction_pool_size = str(max(8, min(16, round(16.0 * scale ** -0.18))))
    construction_checkpoints = str(max(5, min(6, round(6.0 - 0.45 * max(0.0, math.log10(scale))))))
    construction_crossovers = str(max(2, min(4, round(int(construction_pool_size) / 4.0))))
    construction_checkpoint_seconds = str(max(180, min(600, round(300.0 * scale ** 0.22))))
    construction_final_seconds = str(max(360, min(1200, 2 * int(construction_checkpoint_seconds))))
    construction_final_lineages = str(max(4, min(8, round(int(construction_pool_size) / 2.0))))
    elite_pool_size = str(max(8, min(24, round(24.0 * scale ** -0.34))))
    base_active_sa_seeds = max(
        3, min(5, round(5.0 - 1.4 * max(0.0, math.log10(scale)))))

    # RINGARC35.0.2: blend smoothly from the proven 16-group regime at k=100
    # to one independent SA group per usable SA thread at k=50 and below.
    # Reserve the deterministic exact lane first, matching the C++ scheduler,
    # so the configured group count is the count the kernel can actually run.
    exact_lane_enabled = os.environ.get("GRID_EXACT_SWAP_LANE", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    exact_lane_fraction = float(os.environ.get("GRID_EXACT_SWAP_LANE_CPU_FRACTION", "0.125"))
    exact_lane_min = int(os.environ.get("GRID_EXACT_SWAP_LANE_MIN_THREADS", "4"))
    exact_lane_max = int(os.environ.get("GRID_EXACT_SWAP_LANE_MAX_THREADS", "8"))
    exact_lane_threads = 0
    if exact_lane_enabled and cpus >= 24 and exact_lane_fraction > 0.0:
        exact_lane_threads = int(math.floor(exact_lane_fraction * cpus + 0.5))
        exact_lane_threads = max(exact_lane_min, min(exact_lane_max, exact_lane_threads))
        exact_lane_threads = min(exact_lane_threads, cpus - 1)
    sa_thread_budget = max(1, cpus - exact_lane_threads)

    tiny_k_x = max(0.0, min(1.0, (100.0 - float(task.k)) / 50.0))
    tiny_k_blend = tiny_k_x * tiny_k_x * (3.0 - 2.0 * tiny_k_x)
    base_sa_groups = min(16, sa_thread_budget)
    default_sa_groups = int(math.floor(
        base_sa_groups + (sa_thread_budget - base_sa_groups) * tiny_k_blend + 0.5))
    default_sa_groups = max(1, min(sa_thread_budget, default_sa_groups))

    default_active_sa_seeds = int(math.floor(
        base_active_sa_seeds + (8 - base_active_sa_seeds) * tiny_k_blend + 0.5))
    default_active_sa_seeds = max(1, min(8, default_sa_groups, default_active_sa_seeds))
    active_sa_seeds = str(default_active_sa_seeds)
    exploration_seed_limit = str(max(2, min(4, int(active_sa_seeds) - 1)))

    # Validate the exact production preset before any Slurm job is submitted.
    # This deliberately mirrors the binary's supported bounds so launcher/code
    # drift fails immediately in the supervisor instead of after preprocessing.
    active_sa_seed_count = strict_env_int(
        "GRID_ACTIVE_SA_SEEDS", int(active_sa_seeds))
    exploration_seed_count = strict_env_int(
        "GRID_EXPLORATION_SEED_LIMIT", int(exploration_seed_limit))
    sa_group_count = strict_env_int("GRID_SA_GROUPS", default_sa_groups)
    if not 1 <= active_sa_seed_count <= 8:
        raise RuntimeError("GRID_ACTIVE_SA_SEEDS must be in [1,8]")
    if not 0 <= exploration_seed_count <= 8:
        raise RuntimeError("GRID_EXPLORATION_SEED_LIMIT must be in [0,8]")
    if exploration_seed_count > active_sa_seed_count - 1:
        raise RuntimeError(
            "GRID_EXPLORATION_SEED_LIMIT must be <= GRID_ACTIVE_SA_SEEDS - 1")
    if sa_group_count < 1:
        raise RuntimeError("GRID_SA_GROUPS must be positive")
    if active_sa_seed_count > sa_group_count:
        raise RuntimeError(
            "GRID_ACTIVE_SA_SEEDS cannot exceed GRID_SA_GROUPS")

    values.update({
        "GRID_K": str(task.k),
        "GRID_TAU": "{:.17g}".format(task.tau),
        "GRID_TAG": task.tag,
        "GRID_REPLICA": "0",
        "GRID_PROFILE": "balanced",
        "GRID_CANDIDATE_MODE": "vertex",
        "GRID_PREPROCESS_THREADS": str(cpus),
        "GRID_VERIFY_THREADS": str(cpus),
        "GRID_CGAL_VISIBILITY_WORKERS": "0",
        "GRID_CGAL_VISIBILITY_WORKER_CAP": os.environ.get("GRID_CGAL_VISIBILITY_WORKER_CAP", "48"),
        "GRID_PROGRESS_EVERY": os.environ.get("GRID_PROGRESS_EVERY", "1000"),
        "GRID_SA_GROUPS": str(sa_group_count),
        "GRID_SA_SEED": str(seed_base + task.order * 1000 + 1),
        "GRID_SA_PAIR_CACHE_SIZE": os.environ.get("GRID_SA_PAIR_CACHE_SIZE", "0"),
        "GRID_SA_HOT_LIMIT": os.environ.get("GRID_SA_HOT_LIMIT", "8192"),
        "GRID_SA_HOT_FRACTION": os.environ.get("GRID_SA_HOT_FRACTION", "0.80"),
        "GRID_SA_AUDIT_EVERY": os.environ.get("GRID_SA_AUDIT_EVERY", "256"),
        "GRID_SA_AUDIT_SECONDS": os.environ.get("GRID_SA_AUDIT_SECONDS", "30"),
        "GRID_SA_EPOCH_SECONDS": os.environ.get("GRID_SA_EPOCH_SECONDS", "105"),
        "GRID_SA_GROWTH_MIN_SECONDS": os.environ.get("GRID_SA_GROWTH_MIN_SECONDS", "90"),
        "GRID_SA_GROWTH_MAX_SECONDS": os.environ.get("GRID_SA_GROWTH_MAX_SECONDS", "300"),
        "GRID_SA_REPAIR_GRACE_SECONDS": os.environ.get("GRID_SA_REPAIR_GRACE_SECONDS", "30"),
        "GRID_SA_REPAIR_MAX_GRACE_SECONDS": os.environ.get("GRID_SA_REPAIR_MAX_GRACE_SECONDS", "180"),
        "GRID_SA_REPAIR_ADMISSION_FACTOR": os.environ.get("GRID_SA_REPAIR_ADMISSION_FACTOR", "1.15"),
        "GRID_SA_CALIBRATION_SAMPLES": os.environ.get("GRID_SA_CALIBRATION_SAMPLES", "24"),
        "GRID_SA_INITIAL_ACCEPTANCE": os.environ.get("GRID_SA_INITIAL_ACCEPTANCE", "0.20"),
        "GRID_SA_FINAL_ACCEPTANCE": os.environ.get("GRID_SA_FINAL_ACCEPTANCE", "0.004"),
        "GRID_EXACT_SWAP_LANE": os.environ.get("GRID_EXACT_SWAP_LANE", "1"),
        "GRID_EXACT_SWAP_LANE_CPU_FRACTION": os.environ.get("GRID_EXACT_SWAP_LANE_CPU_FRACTION", "0.125"),
        "GRID_EXACT_SWAP_LANE_MIN_THREADS": os.environ.get("GRID_EXACT_SWAP_LANE_MIN_THREADS", "4"),
        "GRID_EXACT_SWAP_LANE_MAX_THREADS": os.environ.get("GRID_EXACT_SWAP_LANE_MAX_THREADS", "8"),
        "GRID_RUIN_SEED": str(seed_base + task.order * 1000 + 2),
        "GRID_FINAL_POLISH_FRACTION": os.environ.get("GRID_FINAL_POLISH_FRACTION", "0.55"),
        "GRID_PORTFOLIO_ONE_SWAP_FRACTION": os.environ.get("GRID_PORTFOLIO_ONE_SWAP_FRACTION", "0.34"),
        "GRID_PORTFOLIO_SA_FRACTION": os.environ.get("GRID_PORTFOLIO_SA_FRACTION", "0.36"),
        "GRID_PORTFOLIO_RUIN_FRACTION": os.environ.get("GRID_PORTFOLIO_RUIN_FRACTION", "0.22"),
        "GRID_PORTFOLIO_CROSSOVER_FRACTION": os.environ.get("GRID_PORTFOLIO_CROSSOVER_FRACTION", "0.08"),
        "GRID_CONSTRUCTION_POOL_SIZE": os.environ.get("GRID_CONSTRUCTION_POOL_SIZE", construction_pool_size),
        "GRID_CONSTRUCTION_POOL_CHECKPOINTS": os.environ.get("GRID_CONSTRUCTION_POOL_CHECKPOINTS", construction_checkpoints),
        "GRID_CONSTRUCTION_POOL_CROSSOVERS": os.environ.get("GRID_CONSTRUCTION_POOL_CROSSOVERS", construction_crossovers),
        "GRID_CONSTRUCTION_POOL_MIN_DISTANCE": os.environ.get("GRID_CONSTRUCTION_POOL_MIN_DISTANCE", "0"),
        "GRID_CONSTRUCTION_POOL_BUDGET_FRACTION": os.environ.get("GRID_CONSTRUCTION_POOL_BUDGET_FRACTION", "0.08"),
        "GRID_CONSTRUCTION_POOL_CHECKPOINT_MAX_SECONDS": os.environ.get("GRID_CONSTRUCTION_POOL_CHECKPOINT_MAX_SECONDS", construction_checkpoint_seconds),
        "GRID_CONSTRUCTION_POOL_FINAL_PROMOTION_MAX_SECONDS": os.environ.get("GRID_CONSTRUCTION_POOL_FINAL_PROMOTION_MAX_SECONDS", construction_final_seconds),
        "GRID_CONSTRUCTION_POOL_FINAL_LINEAGES": os.environ.get("GRID_CONSTRUCTION_POOL_FINAL_LINEAGES", construction_final_lineages),
        "GRID_CONSTRUCTION_POOL_CROSSOVER_FRACTION": os.environ.get("GRID_CONSTRUCTION_POOL_CROSSOVER_FRACTION", "0.08"),
        "GRID_CONSTRUCTION_POOL_CROSSOVER_MAX_SECONDS": os.environ.get("GRID_CONSTRUCTION_POOL_CROSSOVER_MAX_SECONDS", "45"),
        "GRID_ELITE_POOL_SIZE": os.environ.get("GRID_ELITE_POOL_SIZE", elite_pool_size),
        "GRID_ELITE_MIN_DISTANCE": os.environ.get("GRID_ELITE_MIN_DISTANCE", "0"),
        "GRID_ELITE_CROSSOVER_EVERY": os.environ.get("GRID_ELITE_CROSSOVER_EVERY", "2"),
        "GRID_ACTIVE_SA_SEEDS": str(active_sa_seed_count),
        "GRID_PORTFOLIO_SA_MAX_SECONDS": os.environ.get("GRID_PORTFOLIO_SA_MAX_SECONDS", "900"),
        "GRID_EXPLORATION_SEED_LIMIT": str(exploration_seed_count),
        "GRID_EXPLORATION_SCORE_DROP_MAX": os.environ.get("GRID_EXPLORATION_SCORE_DROP_MAX", "4"),
        "GRID_EXPLORATION_MIN_DISTANCE": os.environ.get("GRID_EXPLORATION_MIN_DISTANCE", "0"),
        "GRID_INCUMBENT_CHECKPOINT_SECONDS": os.environ.get("GRID_INCUMBENT_CHECKPOINT_SECONDS", "300"),
        "GRID_EDGE_ENABLE": os.environ.get("GRID_EDGE_ENABLE", "1"),
        "GRID_EDGE_VERTEX_SECONDS": os.environ.get("GRID_EDGE_VERTEX_SECONDS", "0"),
        "GRID_EDGE_SEARCH_RESERVE_SECONDS": os.environ.get("GRID_EDGE_SEARCH_RESERVE_SECONDS", "0"),
        "GRID_EDGE_SEARCH_FRACTION": os.environ.get("GRID_EDGE_SEARCH_FRACTION", "0.30"),
        "GRID_EDGE_SA_PROPOSAL_FRACTION": os.environ.get("GRID_EDGE_SA_PROPOSAL_FRACTION", "0.125"),
        "GRID_EDGE_SA_MIN_PROPOSAL_BATCH": os.environ.get("GRID_EDGE_SA_MIN_PROPOSAL_BATCH", "100000"),
        "GRID_EDGE_SA_MAX_PROPOSAL_BATCH": os.environ.get("GRID_EDGE_SA_MAX_PROPOSAL_BATCH", "500000"),
        "GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS": os.environ.get("GRID_VERTEX_POSTK_SAMPLED_REMOVAL_GROUPS", "2"),
        "GRID_EDGE_SAMPLED_REMOVAL_GROUPS": os.environ.get("GRID_EDGE_SAMPLED_REMOVAL_GROUPS", "4"),
        "GRID_SAMPLED_REMOVAL_SLOTS": os.environ.get("GRID_SAMPLED_REMOVAL_SLOTS", "8"),
        "GRID_SAMPLED_REMOVAL_ANCHORS": os.environ.get("GRID_SAMPLED_REMOVAL_ANCHORS", "2"),
        "GRID_WIGGLE_ENABLE": os.environ.get("GRID_WIGGLE_ENABLE", "1"),
        "GRID_WIGGLE_SECONDS": os.environ.get("GRID_WIGGLE_SECONDS", "1200"),
        "GRID_WIGGLE_STEP_METERS": os.environ.get("GRID_WIGGLE_STEP_METERS", "0.001"),
        "GRID_ALL_K_VALUES": common.get("GRID_ALL_K_VALUES", str(task.k)),
        "GRID_TAU_TOKEN": tau_token(task.tau),
        "EDGE_FOUNDRY_DIR": common.get("EDGE_FOUNDRY_DIR", ""),
        "GRID_RUIN_ATTEMPTS": "1",
        "GRID_EXPERIMENTAL_CP_SAT": "0",
    })
    return values


def submit_cell(envelope: Envelope, common: dict[str, str], partition: str, walltime: str, script: Path, campaign_dir: Path) -> tuple[str | None, str]:
    task = envelope.task
    # Startup fast path: start_v31_24h.sh has already required an empty user
    # queue, so the first foundry + nine first-attempt cells are exactly the
    # permitted ten jobs.  Only retries need an expensive full-user squeue
    # guard because a cancelled predecessor may still be visible briefly.
    if envelope.attempt > 0:
        wait_for_user_job_slot("retry cell {} attempt {}".format(task.tag, envelope.attempt))
    base.clear_markers(campaign_dir, task.tag)
    log_path = campaign_dir / "logs" / (task.tag + ".slurm-%j.log")
    command = [
        "sbatch", "--parsable",
        "--partition={}".format(partition),
        "--job-name=r31-{}-{}".format(task.k, int(round(task.tau * 100))),
        "--time={}".format(walltime),
        "--nodes=1", "--ntasks=1",
        "--cpus-per-task={}".format(envelope.cpus),
        "--mem={}M".format(envelope.mem_mb),
        "--output={}".format(log_path),
        "--error={}".format(log_path),
        "--export={}".format(make_export(make_cell_export(task, common, envelope.cpus))),
    ]
    # Resource envelopes are sized from the latest live fragments, but node
    # placement is always left to Slurm. A basis node is telemetry only.
    command.append(str(script))

    code, text = run_capture(command, timeout=env_int("GRID_SBATCH_TIMEOUT_SECONDS", 90, 20))
    if code != 0:
        return None, text or "sbatch failed"
    job_id = text.split(";", 1)[0].strip().splitlines()[0].strip()
    if not job_id.isdigit():
        return None, text or "unparseable sbatch output"
    return job_id, text


def job_snapshot(job_id: str) -> dict[str, str] | None:
    code, text = run_capture(["scontrol", "show", "job", "-o", job_id], timeout=15)
    if code != 0 or not text:
        return None
    fields = parse_kv_line(text.splitlines()[0])
    return {
        "state": state_base(fields.get("JobState", "UNKNOWN")),
        "reason": fields.get("Reason", "None"),
        "node": short_node(fields.get("NodeList", fields.get("BatchHost", ""))),
        "start": fields.get("StartTime", "Unknown"),
    }


def batch_queue_snapshots(job_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """Return one cheap steady-state Slurm snapshot for many jobs.

    Admission/startup still uses scontrol for detailed handshakes, but a long
    campaign must not issue nine scontrol RPCs every monitor tick on Hendrix's
    small shared gateway.  One squeue query is sufficient once cells are live.
    """
    ids = [str(job_id) for job_id in job_ids if str(job_id).isdigit()]
    if not ids:
        return {}
    code, text = run_capture([
        "squeue", "-h", "-j", ",".join(ids),
        "-o", "%i|%T|%R|%N",
    ], timeout=20)
    if code != 0:
        return {}
    snapshots: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) != 4:
            continue
        job_id, state, reason, node = parts
        snapshots[job_id.strip()] = {
            "state": state_base(state),
            "reason": reason.strip() or "None",
            "node": short_node(node),
            "start": "Unknown",
        }
    return snapshots


def accounting_snapshot(job_id: str) -> dict[str, str] | None:
    # Slurm job IDs can be recycled, and `sacct -j N` may return related or
    # historical rows such as N_0 before the exact N row.  Never classify a
    # monitored job from the first row returned: authenticate JobIDRaw exactly.
    wanted_job_id = str(job_id).strip()
    code, text = run_capture([
        "sacct", "-n", "-X", "-j", wanted_job_id,
        "-o", "JobIDRaw,State,ExitCode,Elapsed,NodeList,MaxRSS", "-P",
    ], timeout=20)
    if code != 0 or not text:
        return None
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 6 or parts[0].strip() != wanted_job_id:
            continue
        return {
            "state": state_base(parts[1]),
            "exit": parts[2],
            "elapsed": parts[3],
            "node": short_node(parts[4]),
            "maxrss": parts[5],
        }
    return None




@dataclass
class FoundryRecord:
    job_id: str = ""
    attempt: int = 0
    state: str = "NOT_SUBMITTED"
    reason: str = ""
    node: str = ""
    cpus: int = 0
    mem_gb: int = 0
    submitted_at: float = 0.0
    updated_at: float = 0.0


def write_foundry_record(campaign_dir: Path, record: FoundryRecord) -> None:
    lines = [
        "attempt\tjob_id\tstate\tnode\tcpus\tmem_gib\treason\tsubmitted_at\tupdated_at",
        "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}".format(
            record.attempt,
            record.job_id,
            record.state,
            record.node,
            record.cpus,
            record.mem_gb,
            record.reason.replace("\t", " ").replace("\n", " ")[:400],
            dt.datetime.fromtimestamp(record.submitted_at).isoformat()
            if record.submitted_at else "",
            dt.datetime.fromtimestamp(record.updated_at).isoformat()
            if record.updated_at else "",
        ),
    ]
    temporary = campaign_dir / "edge_foundry_job.tsv.tmp"
    temporary.write_text("\n".join(lines) + "\n")
    os.replace(temporary, campaign_dir / "edge_foundry_job.tsv")


def submit_foundry_job(
    root: Path,
    campaign_dir: Path,
    common: dict[str, str],
    partition: str,
    walltime: str,
    attempt: int,
) -> str:
    foundry_script = root / "run_v31_edge_foundry.sbatch"
    foundry_cpus = env_int("EDGE_FOUNDRY_CPUS", 48, 1)
    foundry_mem_gb = env_int("EDGE_FOUNDRY_MEM_GB", 512, 32)
    foundry_log = campaign_dir / "logs" / "edge-foundry.slurm-%j.log"
    foundry_export = dict(common)
    foundry_export.update({
        "EDGE_SUBDIVISIONS": os.environ.get("EDGE_SUBDIVISIONS", "1"),
        "EDGE_PROGRESS_EVERY": os.environ.get("EDGE_PROGRESS_EVERY", "500"),
        "EDGE_CGAL_VISIBILITY_WORKERS": os.environ.get("EDGE_CGAL_VISIBILITY_WORKERS", "0"),
        "EDGE_CGAL_VISIBILITY_WORKER_CAP": os.environ.get("EDGE_CGAL_VISIBILITY_WORKER_CAP", "48"),
        "EDGE_FOUNDRY_HORIZON_SECONDS": os.environ.get("EDGE_FOUNDRY_HORIZON_SECONDS", "0"),
    })
    # Attempt 1 is part of the known-empty 9+1 startup fast path.  Later
    # attempts wait for a slot so a retiring failed foundry cannot create an
    # accidental eleventh live/pending user job.
    if attempt > 1:
        wait_for_user_job_slot("edge foundry retry attempt {}".format(attempt))
    command = [
        "sbatch", "--parsable",
        "--partition={}".format(partition),
        "--job-name=r31-edge-foundry-a{}".format(attempt),
        "--time={}".format(walltime),
        "--nodes=1", "--ntasks=1",
        "--cpus-per-task={}".format(foundry_cpus),
        "--mem={}G".format(foundry_mem_gb),
        "--output={}".format(foundry_log),
        "--error={}".format(foundry_log),
        "--export={}".format(make_export(foundry_export)),
        str(foundry_script),
    ]
    code, text = run_capture(
        command, timeout=env_int("GRID_SBATCH_TIMEOUT_SECONDS", 90, 20)
    )
    if code != 0:
        raise RuntimeError("edge foundry submission failed: {}".format(text))
    job_id = text.split(";", 1)[0].strip().splitlines()[0].strip()
    if not job_id.isdigit():
        raise RuntimeError("unparseable edge foundry job id: {}".format(text))
    return job_id


class FoundryMonitor(threading.Thread):
    """Own the tenth job and retry bounded early failures independently."""

    def __init__(
        self,
        root: Path,
        campaign_dir: Path,
        common: dict[str, str],
        partition: str,
        walltime: str,
    ) -> None:
        super().__init__(name="ringarc28-edge-foundry-monitor", daemon=True)
        self.root = root
        self.campaign_dir = campaign_dir
        self.common = common
        self.partition = partition
        self.walltime = walltime
        self.record = FoundryRecord()
        self.stop_event = threading.Event()
        self.campaign_started = time.time()

    def submit_new(self, reason: str) -> str:
        edge_dir = self.campaign_dir / "edge_foundry"
        cache_dir = edge_dir / "cache"
        edge_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            edge_dir / "EDGE_READY.tsv",
            edge_dir / "EDGE_READY.tmp",
            edge_dir / "EDGE_FAILED.tsv",
            edge_dir / "EDGE_FAILED.tmp",
            cache_dir / "edge_visibility_R28.viscache",
            cache_dir / "edge_visibility_R28.scene",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.record.attempt += 1
        self.record.cpus = env_int("EDGE_FOUNDRY_CPUS", 48, 1)
        self.record.mem_gb = env_int("EDGE_FOUNDRY_MEM_GB", 512, 32)
        self.record.node = ""
        self.record.job_id = submit_foundry_job(
            self.root,
            self.campaign_dir,
            self.common,
            self.partition,
            self.walltime,
            self.record.attempt,
        )
        self.record.state = "SUBMITTED"
        self.record.reason = reason
        self.record.submitted_at = time.time()
        self.record.updated_at = self.record.submitted_at
        write_foundry_record(self.campaign_dir, self.record)
        policy = self.campaign_dir / "policy.json"
        if policy.is_file():
            try:
                payload = json.loads(policy.read_text())
                payload["edge_foundry_job_id"] = self.record.job_id
                payload["edge_foundry_attempt"] = self.record.attempt
                temporary = policy.with_name(policy.name + ".tmp")
                temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                os.replace(temporary, policy)
            except (OSError, ValueError) as exc:
                print("WARNING: could not refresh policy.json for foundry retry: {}".format(exc), flush=True)
        history = self.campaign_dir / "edge_foundry_history.tsv"
        with history.open("a") as stream:
            if history.stat().st_size == 0:
                stream.write("attempt\tjob_id\tevent\ttime\treason\n")
            stream.write("{}\t{}\tSUBMITTED\t{}\t{}\n".format(
                self.record.attempt,
                self.record.job_id,
                dt.datetime.now().isoformat(),
                reason.replace("\t", " ").replace("\n", " "),
            ))
        print(
            "Edge foundry attempt {} submitted as job {} ({}).".format(
                self.record.attempt, self.record.job_id, reason
            ),
            flush=True,
        )
        return self.record.job_id

    def stop(self, cancel_active: bool = False) -> None:
        self.stop_event.set()
        if not cancel_active or not self.record.job_id:
            return
        ready = self.campaign_dir / "edge_foundry" / "EDGE_READY.tsv"
        if ready.is_file() and ready.stat().st_size > 0:
            return
        snap = job_snapshot(self.record.job_id)
        if snap is not None and snap["state"] not in (
            TERMINAL_SUCCESS_STATES
            | TERMINAL_FAILURE_STATES
            | TERMINAL_NO_RESTART_STATES
        ):
            print(
                "Cancelling unfinished edge foundry job {} during supervisor shutdown.".format(
                    self.record.job_id
                ),
                flush=True,
            )
            subprocess.call(["scancel", self.record.job_id])

    def run(self) -> None:
        poll_seconds = env_int("GRID_FOUNDRY_MONITOR_POLL_SECONDS", 30, 10)
        max_attempts = env_int("GRID_FOUNDRY_MAX_ATTEMPTS", 3, 1)
        retry_cutoff = env_int("GRID_FOUNDRY_RETRY_CUTOFF_SECONDS", 3600, 0)
        ready = self.campaign_dir / "edge_foundry" / "EDGE_READY.tsv"
        stop_marker = self.campaign_dir / "STOP"
        completed_without_marker_since = 0.0
        publication_grace = env_int("GRID_FOUNDRY_PUBLICATION_GRACE_SECONDS", 30, 0)
        while not self.stop_event.is_set():
            if ready.is_file() and ready.stat().st_size > 0:
                self.record.state = "READY"
                self.record.reason = "authenticated marker published"
                self.record.updated_at = time.time()
                write_foundry_record(self.campaign_dir, self.record)
                print(
                    "EDGE FOUNDRY READY from job {} attempt {}.".format(
                        self.record.job_id, self.record.attempt
                    ),
                    flush=True,
                )
                return
            if stop_marker.exists():
                self.record.state = "STOP_REQUESTED"
                self.record.reason = "campaign STOP marker"
                self.record.updated_at = time.time()
                write_foundry_record(self.campaign_dir, self.record)
                return

            snap = job_snapshot(self.record.job_id) if self.record.job_id else None
            if snap is not None:
                snap_state = snap["state"]
                self.record.state = snap_state
                self.record.reason = snap["reason"]
                self.record.node = snap["node"] or self.record.node
                self.record.updated_at = time.time()
                write_foundry_record(self.campaign_dir, self.record)
                if snap_state not in (
                    TERMINAL_SUCCESS_STATES
                    | TERMINAL_FAILURE_STATES
                    | TERMINAL_NO_RESTART_STATES
                ):
                    self.stop_event.wait(poll_seconds)
                    continue

            acct = accounting_snapshot(self.record.job_id) if self.record.job_id else None
            if acct is None:
                self.record.state = "ACCOUNTING_PENDING"
                self.record.reason = "job absent from squeue; waiting for sacct"
                self.record.updated_at = time.time()
                write_foundry_record(self.campaign_dir, self.record)
                self.stop_event.wait(poll_seconds)
                continue

            state = acct["state"]
            self.record.node = acct["node"] or self.record.node
            if state in TERMINAL_SUCCESS_STATES and not ready.exists():
                if completed_without_marker_since <= 0:
                    completed_without_marker_since = time.time()
                    self.record.state = "PUBLICATION_PENDING"
                    self.record.reason = "job completed; waiting for EDGE_READY.tsv"
                    self.record.updated_at = time.time()
                    write_foundry_record(self.campaign_dir, self.record)
                    self.stop_event.wait(min(poll_seconds, max(1, publication_grace)))
                    continue
                if time.time() - completed_without_marker_since < publication_grace:
                    self.stop_event.wait(poll_seconds)
                    continue
            else:
                completed_without_marker_since = 0.0

            terminal_failure = state in TERMINAL_FAILURE_STATES or (
                state in TERMINAL_SUCCESS_STATES and not ready.exists()
            )
            if terminal_failure:
                reason = "state={} exit={} elapsed={} maxrss={}".format(
                    state, acct["exit"], acct["elapsed"], acct["maxrss"]
                )
                can_retry = (
                    self.record.attempt < max_attempts
                    and time.time() - self.campaign_started <= retry_cutoff
                    and not stop_marker.exists()
                )
                if can_retry:
                    print(
                        "Edge foundry job {} failed early ({}); resubmitting attempt {}/{}.".format(
                            self.record.job_id, reason, self.record.attempt + 1, max_attempts
                        ),
                        flush=True,
                    )
                    try:
                        self.submit_new("automatic retry after " + reason)
                    except Exception as exc:  # noqa: BLE001
                        self.record.state = "RETRY_SUBMISSION_FAILED"
                        self.record.reason = str(exc)
                        self.record.updated_at = time.time()
                        write_foundry_record(self.campaign_dir, self.record)
                        print("FATAL edge-foundry retry submission: {}".format(exc), flush=True)
                        return
                    self.stop_event.wait(poll_seconds)
                    continue
                self.record.state = "FAILED_NO_RETRY"
                self.record.reason = reason
                self.record.updated_at = time.time()
                write_foundry_record(self.campaign_dir, self.record)
                print(
                    "WARNING: edge foundry unavailable after {} attempt(s): {}. "
                    "Nine workers continue vertex-only and still verify exactly.".format(
                        self.record.attempt, reason
                    ),
                    flush=True,
                )
                return

            self.record.state = state
            self.record.reason = "exit={} elapsed={} maxrss={}".format(
                acct["exit"], acct["elapsed"], acct["maxrss"]
            )
            self.record.updated_at = time.time()
            write_foundry_record(self.campaign_dir, self.record)
            self.stop_event.wait(poll_seconds)


def slurm_elapsed_seconds(text: str) -> int:
    """Parse Slurm Elapsed values such as D-HH:MM:SS or HH:MM:SS."""
    value = text.strip()
    if not value:
        return 0
    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return 0
    fields = value.split(":")
    if len(fields) != 3:
        return 0
    try:
        hours, minutes, seconds = (int(field) for field in fields)
    except ValueError:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds

def marker_paths(campaign_dir: Path, tag: str) -> tuple[Path, Path]:
    run_dir = campaign_dir / "runs" / tag
    return run_dir / "startup_ok.tsv", run_dir / "startup_failure.tsv"


def marker_matches(path: Path, job_id: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    return path.read_text().split("\t", 1)[0].strip() == job_id


def cancel_job(job_id: str) -> None:
    if not job_id.isdigit():
        return
    subprocess.call(["scancel", job_id])
    deadline = time.time() + env_int("GRID_CANCEL_WAIT_SECONDS", 90, 5)
    while time.time() < deadline:
        code, text = run_capture(["squeue", "-h", "-j", job_id, "-o", "%i"], timeout=15)
        if code == 0 and not text.strip():
            return
        time.sleep(1)
    raise RuntimeError("job {} remained active after cancellation timeout".format(job_id))


def write_cluster_view(campaign_dir: Path, nodes: list[NodeFragment]) -> None:
    lines = [
        "node\tstate\tcpu_total\tcpu_alloc\tcpu_free\tmem_total_gib\tmem_alloc_gib\tmem_free_gib\tsafe_mem_gib"
    ]
    for node in nodes:
        lines.append("\t".join([
            node.name,
            node.state,
            str(node.cpu_total),
            str(node.cpu_alloc),
            str(node.cpu_free),
            "{:.1f}".format(node.mem_total_mb / 1024.0),
            "{:.1f}".format(node.mem_alloc_mb / 1024.0),
            "{:.1f}".format(node.mem_free_mb / 1024.0),
            "{:.1f}".format(node.safe_mem_mb / 1024.0),
        ]))
    (campaign_dir / "cluster_live.tsv").write_text("\n".join(lines) + "\n")


def write_records(campaign_dir: Path, records: list[CellRecord]) -> None:
    lines = [
        "order\ttag\tk\ttau\tstate\tjob_id\tnode\tbasis_node\tcpus\tmem_gib\tattempt\tstartup_ok\treason"
    ]
    for record in sorted(records, key=lambda item: item.task.order):
        lines.append("\t".join([
            str(record.task.order + 1),
            record.task.tag,
            str(record.task.k),
            "{:.17g}".format(record.task.tau),
            record.state,
            record.job_id,
            record.node,
            record.basis_node,
            str(record.cpus),
            "{:.1f}".format(record.mem_mb / 1024.0) if record.mem_mb else "0.0",
            str(record.attempt),
            "1" if record.startup_ok else "0",
            record.reason.replace("\t", " ").replace("\n", " ")[:300],
        ]))
    temporary = campaign_dir / "jobs.tsv.tmp"
    temporary.write_text("\n".join(lines) + "\n")
    os.replace(temporary, campaign_dir / "jobs.tsv")


def wait_for_startup(job_id: str, task: Task, campaign_dir: Path, seconds: int) -> tuple[bool, str]:
    deadline = time.time() + seconds
    ok_path, failure_path = marker_paths(campaign_dir, task.tag)
    while time.time() < deadline:
        if marker_matches(ok_path, job_id):
            return True, "startup-authenticated"
        if marker_matches(failure_path, job_id):
            return False, "startup-failure"
        snap = job_snapshot(job_id)
        if snap is None:
            return False, "job-disappeared-before-startup"
        if snap["state"] in TERMINAL_FAILURE_STATES:
            return False, "{}:{}".format(snap["state"], snap["reason"])
        time.sleep(env_int("GRID_STARTUP_POLL_SECONDS", 3, 1))
    return False, "startup-timeout"


def admit_task(
    record: CellRecord,
    records: list[CellRecord],
    common: dict[str, str],
    partition: str,
    walltime: str,
    script: Path,
    campaign_dir: Path,
    excluded: set[str],
) -> None:
    admission_seconds = env_int("GRID_ADMISSION_SECONDS", 35, 5)
    floor_admission_seconds = env_int("GRID_FLOOR_ADMISSION_SECONDS", 90, admission_seconds)
    startup_seconds = env_int("GRID_STARTUP_TIMEOUT_SECONDS", 180, 30)
    retry_seconds = env_int("GRID_RETRY_COOLDOWN_SECONDS", 5, 0)
    max_attempts = env_int("GRID_MAX_ADMISSION_ATTEMPTS_PER_CELL", 64, 1)

    previous = [
        item for item in records
        if item.task.order < record.task.order and item.cpus > 0 and item.mem_mb > 0
    ]
    cpu_ceiling = previous[-1].cpus if previous else None
    mem_ceiling_mb = previous[-1].mem_mb if previous else None

    nodes = discover_nodes(partition, excluded)
    write_cluster_view(campaign_dir, nodes)
    envelope = choose_initial_envelope(
        record.task, nodes, cpu_ceiling=cpu_ceiling, mem_ceiling_mb=mem_ceiling_mb
    )
    if envelope is None:
        raise RuntimeError("no live fragment meets the floor for {}".format(record.task.tag))

    for _ in range(max_attempts):
        if (campaign_dir / "STOP").exists():
            raise KeyboardInterrupt

        record.state = "SUBMITTING"
        record.cpus = envelope.cpus
        record.mem_mb = envelope.mem_mb
        record.attempt = envelope.attempt
        record.basis_node = envelope.basis_node
        record.reason = ""
        write_records(campaign_dir, records)

        print(
            "Admitting #{}/9 {} at attempt {}: {}c/{:.1f}G "
            "(largest live basis {})".format(
                record.task.order + 1,
                record.task.tag,
                envelope.attempt,
                envelope.cpus,
                envelope.mem_mb / 1024.0,
                envelope.basis_node,
            ),
            flush=True,
        )
        job_id, message = submit_cell(envelope, common, partition, walltime, script, campaign_dir)
        if not job_id:
            record.state = "SUBMISSION_REJECTED"
            record.reason = message
            write_records(campaign_dir, records)
            print("Submission rejected for {}: {}".format(record.task.tag, message.replace("\n", " ")), flush=True)
            next_envelope = lower_envelope(envelope)
            if next_envelope.cpus == envelope.cpus and next_envelope.mem_mb == envelope.mem_mb:
                time.sleep(max(1, retry_seconds))
                nodes = discover_nodes(partition, excluded)
                write_cluster_view(campaign_dir, nodes)
                refreshed = choose_initial_envelope(
                    record.task, nodes, cpu_ceiling=cpu_ceiling, mem_ceiling_mb=mem_ceiling_mb
                )
                if refreshed is not None and (refreshed.cpus > envelope.cpus or refreshed.mem_mb > envelope.mem_mb):
                    envelope = refreshed
                continue
            envelope = next_envelope
            time.sleep(max(1, retry_seconds))
            continue

        record.job_id = job_id
        record.state = "PENDING"
        record.reason = "submitted"
        write_records(campaign_dir, records)
        print("Submitted {} as job {}.".format(record.task.tag, job_id), flush=True)

        limit = floor_admission_seconds if envelope_at_floor(envelope) else admission_seconds
        deadline = time.time() + limit
        running_snapshot: dict[str, str] | None = None
        while time.time() < deadline:
            snap = job_snapshot(job_id)
            if snap is None:
                record.state = "DISAPPEARED"
                record.reason = "job-disappeared"
                write_records(campaign_dir, records)
                break
            record.state = snap["state"]
            record.reason = snap["reason"]
            record.node = snap["node"]
            write_records(campaign_dir, records)
            if snap["state"] in RUNNING_STATES:
                running_snapshot = snap
                break
            if snap["state"] in TERMINAL_FAILURE_STATES:
                break
            time.sleep(env_int("GRID_ADMISSION_POLL_SECONDS", 3, 1))

        if running_snapshot is not None:
            record.state = "RUNNING_STARTUP"
            record.node = running_snapshot["node"]
            record.reason = running_snapshot["reason"]
            write_records(campaign_dir, records)
            startup_ok, startup_reason = wait_for_startup(
                job_id, record.task, campaign_dir, startup_seconds
            )
            if startup_ok:
                record.state = "RUNNING_AUTHENTICATED"
                record.startup_ok = True
                record.reason = startup_reason
                write_records(campaign_dir, records)
                print(
                    "RUNNING #{}/9: {} job {} on {} with {}c/{:.1f}G".format(
                        record.task.order + 1,
                        record.task.tag,
                        job_id,
                        record.node,
                        record.cpus,
                        record.mem_mb / 1024.0,
                    ),
                    flush=True,
                )
                return
            record.reason = startup_reason
            print("Startup failed for {} job {}: {}".format(record.task.tag, job_id, startup_reason), flush=True)
        else:
            print(
                "Admission timeout for {} job {} after {}s ({}); lowering resources.".format(
                    record.task.tag,
                    job_id,
                    limit,
                    record.reason,
                ),
                flush=True,
            )

        cancel_job(job_id)
        record.job_id = ""
        record.node = ""
        record.startup_ok = False
        record.state = "BACKING_OFF"
        write_records(campaign_dir, records)

        next_envelope = lower_envelope(envelope)
        if next_envelope.cpus == envelope.cpus and next_envelope.mem_mb == envelope.mem_mb:
            # At the floor, keep using the scheduler's full node choice rather
            # than pinning a request to a node with a distant reservation.
            time.sleep(max(1, retry_seconds))
            nodes = discover_nodes(partition, excluded)
            write_cluster_view(campaign_dir, nodes)
            floor_basis = choose_initial_envelope(
                record.task, nodes, cpu_ceiling=cpu_ceiling, mem_ceiling_mb=mem_ceiling_mb
            )
            if floor_basis is not None:
                envelope = Envelope(
                    record.task,
                    floor_basis.basis_node,
                    record.task.min_cpus,
                    record.task.min_mem_mb,
                    envelope.attempt + 1,
                )
            continue
        envelope = next_envelope
        time.sleep(max(1, retry_seconds))

    raise RuntimeError("{} exceeded {} admission attempts".format(record.task.tag, max_attempts))



def wave_records(records: list[CellRecord]) -> list[list[CellRecord]]:
    """Return strict descending-k waves, tau descending inside each wave."""
    result: list[list[CellRecord]] = []
    for k_value in sorted({item.task.k for item in records}, reverse=True):
        wave = [item for item in records if item.task.k == k_value]
        wave.sort(key=lambda item: (-item.task.tau, item.task.order))
        if len(wave) != 3:
            raise RuntimeError("expected three tau cells for k={}".format(k_value))
        result.append(wave)
    if len(result) != 3:
        raise RuntimeError("expected exactly three k waves")
    return result


def predecessor_ceiling(record: CellRecord, records: list[CellRecord]) -> tuple[int | None, int | None]:
    previous = [
        item for item in records
        if item.task.order < record.task.order and item.cpus > 0 and item.mem_mb > 0
    ]
    if not previous:
        return None, None
    predecessor = previous[-1]
    return predecessor.cpus, predecessor.mem_mb


def choose_record_envelope(
    record: CellRecord,
    records: list[CellRecord],
    nodes: list[NodeFragment],
    used_basis_nodes: set[str],
) -> Envelope | None:
    """Choose the largest distinct live fragment compatible with current caps."""
    cpu_ceiling, mem_ceiling = predecessor_ceiling(record, records)
    if record.cpus > 0:
        cpu_ceiling = record.cpus if cpu_ceiling is None else min(cpu_ceiling, record.cpus)
    if record.mem_mb > 0:
        mem_ceiling = record.mem_mb if mem_ceiling is None else min(mem_ceiling, record.mem_mb)

    for node in nodes:
        if node.name in used_basis_nodes:
            continue
        envelope = choose_initial_envelope(
            record.task,
            [node],
            cpu_ceiling=cpu_ceiling,
            mem_ceiling_mb=mem_ceiling,
        )
        if envelope is None:
            continue
        envelope.attempt = record.attempt
        used_basis_nodes.add(node.name)
        return envelope
    return None


def lower_record_envelope(record: CellRecord) -> bool:
    """Lower CPU and RAM once. Return False only when already at the floor."""
    if record.cpus <= 0 or record.mem_mb <= 0:
        return False
    current = Envelope(
        record.task,
        record.basis_node,
        record.cpus,
        record.mem_mb,
        record.attempt,
    )
    lowered = lower_envelope(current)
    if lowered.cpus == current.cpus and lowered.mem_mb == current.mem_mb:
        return False
    record.cpus = lowered.cpus
    record.mem_mb = lowered.mem_mb
    record.attempt = lowered.attempt
    record.basis_node = ""
    return True


def nonblocking_startup_status(record: CellRecord, campaign_dir: Path) -> tuple[str, str]:
    ok_path, failure_path = marker_paths(campaign_dir, record.task.tag)
    if marker_matches(ok_path, record.job_id):
        return "ok", "startup-authenticated"
    if marker_matches(failure_path, record.job_id):
        return "failed", "startup-failure"
    return "waiting", "startup-marker-pending"


def cancel_and_backoff(record: CellRecord, campaign_dir: Path, reason: str) -> None:
    job_id = record.job_id
    if job_id:
        cancel_job(job_id)
    record.job_id = ""
    record.node = ""
    record.startup_ok = False
    record.submitted_at = 0.0
    record.running_since = 0.0
    record.reason = reason
    if lower_record_envelope(record):
        record.state = "BACKING_OFF"
        print(
            "Backoff {} -> {}c/{:.1f}G after {}".format(
                record.task.tag,
                record.cpus,
                record.mem_mb / 1024.0,
                reason,
            ),
            flush=True,
        )
    else:
        record.state = "WAITING_AT_FLOOR"
        print(
            "{} is already at its safety floor {}c/{:.1f}G after {}; "
            "the next attempt will retain that floor.".format(
                record.task.tag,
                record.task.min_cpus,
                record.task.min_mem_mb / 1024.0,
                reason,
            ),
            flush=True,
        )
    base.clear_markers(campaign_dir, record.task.tag)


def admit_wave(
    wave_index: int,
    wave: list[CellRecord],
    records: list[CellRecord],
    common: dict[str, str],
    partition: str,
    walltime: str,
    script: Path,
    campaign_dir: Path,
    excluded: set[str],
) -> None:
    """Submit a whole k wave, then keep slimming delayed cells until all run."""
    admission_seconds = env_int("GRID_ADMISSION_SECONDS", 45, 5)
    floor_admission_seconds = env_int(
        "GRID_FLOOR_ADMISSION_SECONDS", 120, admission_seconds
    )
    startup_seconds = env_int("GRID_STARTUP_TIMEOUT_SECONDS", 180, 30)
    retry_seconds = env_int("GRID_RETRY_COOLDOWN_SECONDS", 5, 0)
    poll_seconds = env_int("GRID_ADMISSION_POLL_SECONDS", 3, 1)
    cluster_refresh_seconds = env_int("GRID_CLUSTER_STARTUP_REFRESH_SECONDS", 5, 1)
    max_attempts = env_int("GRID_MAX_ADMISSION_ATTEMPTS_PER_CELL", 64, 1)
    last_cluster_refresh = 0.0
    latest_nodes: list[NodeFragment] = []

    k_value = wave[0].task.k
    tau_text = ", ".join("{:.6g}".format(item.task.tau) for item in wave)
    print(
        "=== WAVE {}/3: k={} (tau {}) ===".format(
            wave_index, k_value, tau_text
        ),
        flush=True,
    )

    while True:
        if (campaign_dir / "STOP").exists():
            raise KeyboardInterrupt
        now = time.time()
        if now - last_cluster_refresh >= cluster_refresh_seconds:
            try:
                latest_nodes = discover_nodes(partition, excluded)
                write_cluster_view(campaign_dir, latest_nodes)
                last_cluster_refresh = now
            except RuntimeError as exc:
                # Cluster telemetry must never kill an otherwise viable startup.
                print("WARNING: startup cluster-view refresh failed: {}".format(exc), flush=True)
        if all(item.startup_ok for item in wave):
            print(
                "WAVE {}/3 k={} COMPLETE: all three jobs RUNNING and authenticated.".format(
                    wave_index, k_value
                ),
                flush=True,
            )
            return

        waiting = [item for item in wave if not item.startup_ok and not item.job_id]
        if waiting:
            nodes = latest_nodes
            if not nodes:
                nodes = discover_nodes(partition, excluded)
                latest_nodes = nodes
                write_cluster_view(campaign_dir, nodes)
                last_cluster_refresh = time.time()
            used_basis_nodes: set[str] = set()
            planned: list[tuple[CellRecord, Envelope]] = []

            # Descending tau is already the wave order. Planning with distinct
            # basis fragments makes the requested envelopes descend in the same
            # order, even though jobs are unpinned by default.
            for record in waiting:
                if record.attempt >= max_attempts:
                    raise RuntimeError(
                        "{} exceeded {} wave-admission attempts".format(
                            record.task.tag, max_attempts
                        )
                    )
                envelope = choose_record_envelope(
                    record, records, nodes, used_basis_nodes
                )
                while envelope is None and lower_record_envelope(record):
                    envelope = choose_record_envelope(
                        record, records, nodes, used_basis_nodes
                    )
                if envelope is None:
                    record.state = "WAITING_FOR_FRAGMENT"
                    record.reason = "no current live fragment meets safety floor"
                    continue
                planned.append((record, envelope))

            if planned:
                print(
                    "Wave k={} admission scan: {} waiting, {} live fragments, "
                    "{} submissions".format(
                        k_value, len(waiting), len(nodes), len(planned)
                    ),
                    flush=True,
                )

            for record, envelope in planned:
                record.state = "SUBMITTING"
                record.cpus = envelope.cpus
                record.mem_mb = envelope.mem_mb
                record.attempt = envelope.attempt
                record.basis_node = envelope.basis_node
                record.reason = "wave-planned"
                write_records(campaign_dir, records)

                job_id, message = submit_cell(
                    envelope, common, partition, walltime, script, campaign_dir
                )
                if not job_id:
                    record.state = "SUBMISSION_REJECTED"
                    record.reason = message
                    print(
                        "Wave submission rejected for {} at {}c/{:.1f}G: {}".format(
                            record.task.tag,
                            record.cpus,
                            record.mem_mb / 1024.0,
                            message.replace("\n", " "),
                        ),
                        flush=True,
                    )
                    lower_record_envelope(record)
                    continue

                record.job_id = job_id
                record.state = "PENDING"
                record.reason = "submitted"
                record.submitted_at = time.time()
                record.running_since = 0.0
                write_records(campaign_dir, records)
                print(
                    "Submitted W{}/3 {} job {} at {}c/{:.1f}G "
                    "(basis {}).".format(
                        wave_index,
                        record.task.tag,
                        job_id,
                        record.cpus,
                        record.mem_mb / 1024.0,
                        record.basis_node,
                    ),
                    flush=True,
                )

        changed = False
        running_count = 0
        pending_count = 0
        for record in wave:
            if record.startup_ok:
                running_count += 1
                continue
            if not record.job_id:
                continue

            snap = job_snapshot(record.job_id)
            if snap is None:
                acct = accounting_snapshot(record.job_id)
                if acct and acct["state"] in TERMINAL_FAILURE_STATES:
                    cancel_and_backoff(
                        record, campaign_dir, "terminal-{}".format(acct["state"])
                    )
                    changed = True
                continue

            record.state = snap["state"]
            record.reason = snap["reason"]
            record.node = snap["node"] or record.node

            if snap["state"] in RUNNING_STATES:
                if record.running_since <= 0:
                    record.running_since = time.time()
                startup_status, startup_reason = nonblocking_startup_status(
                    record, campaign_dir
                )
                if startup_status == "ok":
                    record.state = "RUNNING_AUTHENTICATED"
                    record.startup_ok = True
                    record.reason = startup_reason
                    running_count += 1
                    print(
                        "RUNNING W{}/3 {} job {} on {} with {}c/{:.1f}G".format(
                            wave_index,
                            record.task.tag,
                            record.job_id,
                            record.node,
                            record.cpus,
                            record.mem_mb / 1024.0,
                        ),
                        flush=True,
                    )
                elif startup_status == "failed":
                    cancel_and_backoff(record, campaign_dir, startup_reason)
                    changed = True
                elif time.time() - record.running_since > startup_seconds:
                    cancel_and_backoff(
                        record, campaign_dir, "startup-handshake-timeout"
                    )
                    changed = True
                continue

            if snap["state"] in TERMINAL_FAILURE_STATES:
                cancel_and_backoff(
                    record,
                    campaign_dir,
                    "{}:{}".format(snap["state"], snap["reason"]),
                )
                changed = True
                continue

            pending_count += 1
            elapsed = time.time() - record.submitted_at
            current = Envelope(
                record.task,
                record.basis_node,
                record.cpus,
                record.mem_mb,
                record.attempt,
            )
            limit = (
                floor_admission_seconds
                if envelope_at_floor(current)
                else admission_seconds
            )
            if elapsed >= limit:
                # Competition policy deliberately backs off Priority as well as
                # Resources. The goal is a prompt start, not queue-age retention.
                cancel_and_backoff(
                    record,
                    campaign_dir,
                    "{} pending {:.0f}s".format(record.reason, elapsed),
                )
                changed = True

        write_records(campaign_dir, records)
        summary = "Wave k={} status: running_ok={}/3 active={} pending={} waiting={}".format(
            k_value,
            sum(1 for item in wave if item.startup_ok),
            sum(1 for item in wave if item.job_id),
            pending_count,
            sum(1 for item in wave if not item.startup_ok and not item.job_id),
        )
        print(summary, flush=True)

        if all(item.startup_ok for item in wave):
            continue
        if changed:
            time.sleep(max(1, retry_seconds))
        else:
            time.sleep(poll_seconds)


@dataclass(frozen=True)
class VerifiedBlockSnapshot:
    block_bytes: bytes
    score: int
    sha256: str
    generation: str


def read_authenticated_verified_block(
    task: Task,
    run_dir: Path,
) -> VerifiedBlockSnapshot | None:
    """Return one immutable, authenticated block snapshot or ``None``.

    The marker is read before and after the block/verification payloads.  A
    concurrent publication therefore either yields one complete generation or
    fails closed.  Callers consume the returned bytes rather than reopening the
    mutable path, eliminating the post-authentication TOCTOU window.
    """
    marker = run_dir / "VERIFIED_OK.tsv"
    block = run_dir / "submission_block.txt"
    verification = run_dir / "cgal_verification.txt"
    if not marker.is_file() or not block.is_file() or not verification.is_file():
        return None
    try:
        marker_before = marker.read_bytes()
        fields = marker_before.decode("utf-8").strip().split("\t")
        if len(fields) != 6:
            return None
        revision, k_text, tau_text, score_text, expected_sha, generation = fields
        if revision != EXPECTED_REVISION:
            return None
        if int(k_text) != task.k or abs(float(tau_text) - task.tau) > 1e-12:
            return None

        block_bytes = block.read_bytes()
        verification_bytes = verification.read_bytes()
        marker_after = marker.read_bytes()
        if marker_before != marker_after:
            return None
        if hashlib.sha256(block_bytes).hexdigest() != expected_sha:
            return None

        lines = block_bytes.decode("utf-8").splitlines()
        if len(lines) != 3:
            return None
        values: dict[str, str] = {}
        for line in verification_bytes.decode("utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        score = int(score_text)
        if values.get("feasible") != "yes":
            return None
        if int(values.get("selected", "-1")) != task.k:
            return None
        if int(values.get("expected_k", "-1")) != task.k:
            return None
        if int(values.get("real_score", "-1")) != score:
            return None
    except (OSError, UnicodeError, ValueError):
        return None
    return VerifiedBlockSnapshot(
        block_bytes=block_bytes,
        score=score,
        sha256=expected_sha,
        generation=generation,
    )


def authenticated_verified_block(task: Task, run_dir: Path) -> bool:
    return read_authenticated_verified_block(task, run_dir) is not None


def refresh_emergency_solutions(
    records: list[CellRecord],
    campaign_dir: Path,
) -> bool:
    """Atomically assemble the best available block for all nine cells.

    Verified final blocks are preferred. Otherwise use each worker's monotonic
    best-ever cache-exact checkpoint. The emergency file is deliberately named
    and accompanied by provenance so it cannot be mistaken for the final
    independent-CGAL-verified package.
    """
    chosen: list[tuple[Task, Path, bool, str]] = []
    pair_re = re.compile(
        r"^\(\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*,\s*(\d+)\s*\)$"
    )
    for record in sorted(records, key=lambda item: (item.task.tau, item.task.k)):
        run_dir = campaign_dir / "runs" / record.task.tag
        verified_block = run_dir / "submission_block.txt"
        checkpoint_block = run_dir / "best_cache_exact_submission_block.txt"
        verified_snapshot = read_authenticated_verified_block(record.task, run_dir)
        if verified_snapshot is not None:
            block_path = verified_block
            verified = True
            try:
                raw = verified_snapshot.block_bytes.decode("utf-8")
            except UnicodeError:
                return False
        elif checkpoint_block.is_file():
            block_path = checkpoint_block
            verified = False
            try:
                raw = block_path.read_text(encoding="utf-8")
            except OSError:
                return False
        else:
            return False
        lines = raw.splitlines()
        if len(lines) != 3:
            return False
        match = pair_re.fullmatch(lines[0].strip())
        if match is None:
            return False
        tau = float(match.group(1))
        k = int(match.group(2))
        if k != record.task.k or abs(tau - record.task.tau) > 1e-12:
            return False
        # Preserve an intentionally empty third line (no claimed buildings).
        # rstrip("\n") would collapse a valid three-line block to two lines.
        normalized_block = "\n".join(lines)
        chosen.append((record.task, block_path, verified, normalized_block))

    if len(chosen) != 9:
        return False
    output = campaign_dir / "emergency_solutions.txt"
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        "\n".join(item[3] for item in chosen) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    manifest = {
        "schema": "ringarc28.emergency-solutions.v1",
        "revision": EXPECTED_REVISION,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "independent_cgal_verified_cells": sum(1 for item in chosen if item[2]),
        "warning": (
            "Only revision/hash/score/cardinality-authenticated VERIFIED_OK blocks "
            "are marked independently verified. Other blocks are cache-exact "
            "emergency checkpoints awaiting independent CGAL verification."
        ),
        "cells": [
            {
                "k": task.k,
                "tau": task.tau,
                "tag": task.tag,
                "source": str(path),
                "independent_cgal_verified": verified,
            }
            for task, path, verified, _ in chosen
        ],
    }
    manifest_path = campaign_dir / "emergency_solutions_manifest.json"
    manifest_temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(manifest_temporary, manifest_path)
    return True


def monitor_jobs(
    records: list[CellRecord],
    common: dict[str, str],
    partition: str,
    walltime: str,
    script: Path,
    campaign_dir: Path,
    root: Path,
    excluded: set[str],
) -> None:
    restart_counts = {record.task.tag: 0 for record in records}
    last_emergency_refresh = 0.0
    while True:
        now = time.time()
        if now - last_emergency_refresh >= env_int(
            "GRID_EMERGENCY_REFRESH_SECONDS", 300, 60
        ):
            if refresh_emergency_solutions(records, campaign_dir):
                print(
                    "Emergency nine-cell solutions file refreshed atomically.",
                    flush=True,
                )
            last_emergency_refresh = now
        if (campaign_dir / "STOP").exists():
            print("STOP marker detected; no jobs will be restarted.", flush=True)
            return
        terminal = 0
        queue_view = batch_queue_snapshots(
            record.job_id for record in records if record.job_id
        )
        for record in records:
            if not record.job_id:
                continue
            snap = queue_view.get(record.job_id)
            if snap is not None:
                record.state = snap["state"]
                record.reason = snap["reason"]
                record.node = snap["node"] or record.node
                continue
            acct = accounting_snapshot(record.job_id)
            if acct is None:
                continue
            record.state = acct["state"]
            record.node = acct["node"] or record.node
            record.reason = "exit={} elapsed={} maxrss={}".format(
                acct["exit"], acct["elapsed"], acct["maxrss"]
            )
            if acct["state"] in TERMINAL_SUCCESS_STATES:
                terminal += 1
                continue
            if acct["state"] in TERMINAL_NO_RESTART_STATES:
                record.reason = (
                    "terminal-without-verified-completion: " + record.reason
                )
                terminal += 1
                continue
            if acct["state"] not in TERMINAL_FAILURE_STATES:
                continue

            runtime_seconds = slurm_elapsed_seconds(acct["elapsed"])
            restart_cutoff = env_int(
                "GRID_RUNTIME_RESTART_CUTOFF_SECONDS", 900, 0
            )
            if runtime_seconds > restart_cutoff:
                record.state = "LATE_RUNTIME_FAILURE_NO_RESTART"
                record.reason = (
                    "preserving emergency incumbent; runtime {}s exceeded "
                    "restart cutoff {}s: ".format(runtime_seconds, restart_cutoff)
                    + record.reason
                )
                terminal += 1
                continue

            restart_counts[record.task.tag] += 1
            if restart_counts[record.task.tag] > env_int("GRID_MAX_CELL_RESTARTS", 1, 0):
                record.state = "RESTART_LIMIT_EXCEEDED"
                terminal += 1
                continue

            print(
                "Restarting early failed cell {} after {} (runtime={}s).".format(
                    record.task.tag, acct["state"], runtime_seconds
                ),
                flush=True,
            )
            record.job_id = ""
            record.startup_ok = False
            admit_task(
                record,
                records,
                common,
                partition,
                walltime,
                script,
                campaign_dir,
                excluded,
            )
        write_records(campaign_dir, records)
        if terminal == 9:
            refresh_emergency_solutions(records, campaign_dir)
            (campaign_dir / "ALL_NINE_TERMINAL.tsv").write_text(
                "time\tterminal\n{}\t9\n".format(dt.datetime.now().isoformat())
            )
            print(
                "All nine cells terminal. No automatic summary Slurm job is submitted; "
                "run ./summarize_v31_campaign.sh post-hoc.",
                flush=True,
            )
            return
        time.sleep(env_int("GRID_MONITOR_POLL_SECONDS", 30, 10))



def main() -> int:
    root = Path(os.environ.get("ROOT", os.getcwd())).resolve()
    partition = os.environ.get("GRID_PARTITION", "gpu")
    walltime = os.environ.get("GRID_SLURM_TIME", "1-00:00:00")
    grid_name = os.environ.get("GRID_NAME", "r31_fullrun_freeze_9plus1")
    horizon = os.environ.get("GRID_HORIZON", "23h")
    env_dir = os.environ.get("ENV", "/home/tdz894/envs/gisvis")
    input_candidate = Path(os.environ.get("INPUT", str(root / "data/la_large.geojson"))).expanduser()
    try:
        input_candidate = input_candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError("INPUT does not exist: {}".format(input_candidate)) from exc
    if not input_candidate.is_file() or input_candidate.stat().st_size <= 0:
        raise RuntimeError("INPUT must be a non-empty regular file: {}".format(input_candidate))
    input_path = str(input_candidate)
    if "/absolute/path/to/" in input_path or "/path/to/" in input_path:
        raise RuntimeError("INPUT still contains a placeholder path: {}".format(input_path))
    building_id_mode = os.environ.get("BUILDING_ID_MODE", "source")
    if building_id_mode not in {"source", "internal"}:
        raise RuntimeError("BUILDING_ID_MODE must be source or internal")
    ogrinfo = Path(env_dir) / "bin" / "ogrinfo"
    if ogrinfo.is_file():
        code, text = run_capture([str(ogrinfo), "-ro", "-so", input_path], timeout=60)
        if code != 0:
            raise RuntimeError("GDAL could not open INPUT {}: {}".format(input_path, text))

    if os.environ.get("GRID_REQUIRE_EMPTY_USER_QUEUE", "1") == "1":
        existing = active_user_jobs()
        if existing:
            raise RuntimeError(
                "launch requires an empty personal Slurm queue:\n{}".format("\n".join(existing))
            )

    campaign_id = os.environ.get("CAMPAIGN_ID", time.strftime("%Y%m%d_%H%M%S"))
    campaign_dir = Path(os.environ.get(
        "CAMPAIGN_DIR",
        str(root / "output/portfolio_annealer_edgepool_exact_gate_9plus1" / grid_name / campaign_id),
    )).resolve()
    (campaign_dir / "logs").mkdir(parents=True, exist_ok=True)
    (campaign_dir / "runs").mkdir(parents=True, exist_ok=True)

    campaign_executable, campaign_sha = base.prepare_campaign_executable(
        root, campaign_dir, EXPECTED_REVISION
    )
    (campaign_dir / "campaign_binaries.tsv").write_text(
        "role\trevision\tsha256\tpath\n"
        "single_exact_search_verify\t{}\t{}\t{}\n".format(
            EXPECTED_REVISION, campaign_sha, campaign_executable,
        )
    )
    latest_root = root / "output/portfolio_annealer_edgepool_exact_gate_9plus1" / grid_name
    latest_root.mkdir(parents=True, exist_ok=True)
    (latest_root / "latest_campaign").write_text(str(campaign_dir) + "\n")
    (campaign_dir / "INPUT_RESOLVED.tsv").write_text(
        "input\tsize_bytes\tbuilding_id_mode\tbuilding_id_field\n"
        "{}\t{}\t{}\t{}\n".format(
            input_path, input_candidate.stat().st_size, building_id_mode,
            os.environ.get("BUILDING_ID_FIELD", "id"),
        )
    )

    common = {
        "ROOT": str(root),
        "DATA_ROOT": os.environ.get("DATA_ROOT", str(root)),
        "ENV": env_dir,
        "INPUT": input_path,
        "CAMPAIGN_DIR": str(campaign_dir),
        "EDGE_FOUNDRY_DIR": str(campaign_dir / "edge_foundry"),
        "GRID_NAME": grid_name,
        "GRID_HORIZON": horizon,
        "GRID_ALL_K_VALUES": ",".join(str(v) for v in sorted(parse_int_csv("GRID_K_VALUES", "500,5000,10000"))),
        "GRID_VERIFY": os.environ.get("GRID_VERIFY", "0"),
        "GRID_CANDIDATE_MODE": "vertex",
        "GRID_M": os.environ.get("GRID_M", "0.75"),
        "EDGE_REFERENCE_K": str(max(parse_int_csv("GRID_K_VALUES", "500,5000,10000"))),
        "EDGE_REFERENCE_TAU": "{:.17g}".format(max(parse_float_csv("GRID_TAU_VALUES", "0.25,0.50,0.75"))),
        "EDGE_REFERENCE_M": os.environ.get("GRID_M", "0.75"),
        "GRID_SA_START": os.environ.get("GRID_SA_START", "1.25"),
        "GRID_SA_END": os.environ.get("GRID_SA_END", "0.005"),
        "GRID_STARTUP_TIMEOUT_SECONDS": os.environ.get("GRID_STARTUP_TIMEOUT_SECONDS", "180"),
        "GRID_EXPECTED_REVISION": EXPECTED_REVISION,
        "GRID_EXECUTABLE": str(campaign_executable),
        "GRID_EXECUTABLE_MANIFEST": str(campaign_executable) + ".revision",
        "BUILDING_ID_MODE": building_id_mode,
        "BUILDING_ID_FIELD": os.environ.get("BUILDING_ID_FIELD", "id"),
        "GRID_SUBMISSION_CLAIM_SLACK_METERS": os.environ.get("GRID_SUBMISSION_CLAIM_SLACK_METERS", "0.001"),
    }
    foundry_monitor = FoundryMonitor(
        root, campaign_dir, common, partition, walltime
    )
    foundry_job_id = foundry_monitor.submit_new("initial submission")
    foundry_monitor.start()

    tasks = task_list()
    records = [CellRecord(task=task) for task in tasks]
    script = root / "run_v31_vertex_cell.sbatch"
    excluded = set(filter(None, os.environ.get("GRID_EXCLUDE_NODES", "").split(",")))

    print("Campaign: {}".format(campaign_dir), flush=True)
    print("Resolved INPUT: {} ({} bytes)".format(input_path, input_candidate.stat().st_size), flush=True)
    print("Edge foundry job: {}".format(foundry_job_id), flush=True)
    print("Policy: nine independent jobs in three runtime-k explore/harvest waves", flush=True)
    print("Wave order: " + " -> ".join("k={}".format(v) for v in sorted({t.k for t in tasks}, reverse=True)), flush=True)
    print("Within-wave order: " + " -> ".join("tau={:.6g}".format(v) for v in sorted({t.tau for t in tasks}, reverse=True)), flush=True)
    print("Full order: " + " -> ".join(task.tag for task in tasks), flush=True)
    print("Jobs are unpinned by default; Slurm chooses any fitting live node.", flush=True)
    print("Worker allocation: fixed {} GiB RAM; CPU maximized within k-scaled wave caps.".format(
        env_int("GRID_WORKER_MEM_GB", 128, 64)
    ), flush=True)
    print("Edge foundry allocation: {} CPUs / {} GiB RAM.".format(
        env_int("EDGE_FOUNDRY_CPUS", 48, 1),
        env_int("EDGE_FOUNDRY_MEM_GB", 512, 32),
    ), flush=True)
    print("Pinned revision: {}".format(EXPECTED_REVISION), flush=True)
    print("Single executable SHA256={}".format(campaign_sha), flush=True)

    (campaign_dir / "SUPERVISOR_RUNNING.tsv").write_text(
        "pid\tstarted\n{}\t{}\n".format(os.getpid(), dt.datetime.now().isoformat())
    )
    (campaign_dir / "policy.json").write_text(json.dumps({
        "revision": EXPECTED_REVISION,
        "single_executable_sha256": campaign_sha,
        "edge_foundry_job_id": foundry_job_id,
        "edge_foundry_dir": str(campaign_dir / "edge_foundry"),
        "task_order": [task.tag for task in tasks],
        "waves": [[item.task.tag for item in wave] for wave in wave_records(records)],
        "placement": "slurm-unpinned",
        "worker_mem_gb": env_int("GRID_WORKER_MEM_GB", 128, 64),
        "edge_foundry_cpus": env_int("EDGE_FOUNDRY_CPUS", 48, 1),
        "edge_foundry_mem_gb": env_int("EDGE_FOUNDRY_MEM_GB", 512, 32),
        "cluster_startup_refresh_seconds": env_int("GRID_CLUSTER_STARTUP_REFRESH_SECONDS", 5, 1),
        "admission_seconds": env_int("GRID_ADMISSION_SECONDS", 35, 5),
        "floor_admission_seconds": env_int("GRID_FLOOR_ADMISSION_SECONDS", 90, 5),
        "cpu_step": env_int("GRID_CPU_STEP", 8, 1),
        "cpu_step_percent": env_float("GRID_CPU_STEP_PERCENT", 8.0, 0.0),
        "mem_step_gb": env_int("GRID_MEM_STEP_GB", 16, 1),
        "mem_step_percent": env_float("GRID_MEM_STEP_PERCENT", 4.0, 0.0),
    }, indent=2, sort_keys=True) + "\n")
    write_records(campaign_dir, records)

    try:
        for wave_index, wave in enumerate(wave_records(records), 1):
            admit_wave(
                wave_index,
                wave,
                records,
                common,
                partition,
                walltime,
                script,
                campaign_dir,
                excluded,
            )
        marker = campaign_dir / "ALL_NINE_RUNNING.tsv"
        marker.write_text(
            "time\trunning_authenticated\n{}\t9\n".format(dt.datetime.now().isoformat())
        )
        print("ALL NINE CELLS ARE RUNNING AND AUTHENTICATED.", flush=True)
        enter_steady_state_niceness()
        monitor_jobs(
            records,
            common,
            partition,
            walltime,
            script,
            campaign_dir,
            root,
            excluded,
        )
    finally:
        foundry_monitor.stop(cancel_active=True)
        foundry_monitor.join(timeout=5)
        try:
            (campaign_dir / "SUPERVISOR_RUNNING.tsv").unlink()
        except FileNotFoundError:
            pass
        (campaign_dir / "SUPERVISOR_EXIT.tsv").write_text(
            "time\n{}\n".format(dt.datetime.now().isoformat())
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr, flush=True)
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        print("FATAL: {}".format(exc), file=sys.stderr, flush=True)
        raise
