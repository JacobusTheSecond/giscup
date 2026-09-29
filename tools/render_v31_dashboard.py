#!/usr/bin/env python3
"""Rich, low-load RINGARC31 campaign dashboard.

The renderer is intentionally scheduler-blind: it never calls squeue/scontrol.
It reads the supervisor's cached jobs.tsv plus bounded tails of the nine worker
logs and foundry log.  The supervisor is the only long-lived process that polls
Slurm, roughly once per minute in steady state.  This makes a 10-second visual
refresh safe on Hendrix's small shared gateway.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

SCORE_RE = re.compile(r"score=(\d+)/(\d+)")
BEST_RE = re.compile(r"best_score=(\d+)")
CURRENT_RE = re.compile(r"current_score=(\d+)")
INTEREST = (
    "Portfolio cycle", "Simulated annealing", "SA role=", "primary_downhill",
    "primary_drawdown", "escape_wins", "Ruin/recreate", "frontier-building",
    "coverage-neighborhood", "Elite crossover", "PHASE 2", "edge foundry",
    "EDGE FOUNDRY", "wiggle", "Wiggle", "checkpoint", "local optimum",
    "Exact 1-swap", "Submission block", "claim-only slack", "ERROR", "WARNING",
)


def bounded_tail(path: Path, max_bytes: int = 32_768) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_tsv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh, delimiter="\t"))
    except OSError:
        return []


def latest_log(campaign: Path, tag: str) -> Path | None:
    direct = campaign / "logs" / f"{tag}.log"
    if direct.is_file():
        return direct
    matches = list((campaign / "logs").glob(f"{tag}.slurm-*.log"))
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def marker_summary(run_dir: Path) -> str:
    flags = []
    for name, label in [
        ("startup_ok.tsv", "START"),
        ("best_cache_exact_submission_block.txt", "BEST"),
        ("submission_block.txt", "BLOCK"),
        ("VERIFIED_OK.tsv", "CGAL"),
    ]:
        p = run_dir / name
        if p.is_file() and p.stat().st_size > 0:
            flags.append(label)
    return "+".join(flags) if flags else "-"


def score_from_text(text: str) -> str:
    matches = SCORE_RE.findall(text)
    if matches:
        return f"{matches[-1][0]}/{matches[-1][1]}"
    best = BEST_RE.findall(text)
    current = CURRENT_RE.findall(text)
    if best or current:
        b = best[-1] if best else "?"
        c = current[-1] if current else "?"
        return f"b{b}/c{c}"
    return "-"


def phase_from(run_dir: Path, text: str) -> str:
    handoff = run_dir / "edge_handoff.tsv"
    if handoff.is_file():
        try:
            fields = handoff.read_text(errors="replace").strip().split("\t")
            if fields:
                if fields[0] == "visibility":
                    if "=== phase 2c/3" in text.lower():
                        return "WIGGLE"
                    return "EDGE"
                if fields[0] == "vertex":
                    return "VTX-LATE"
        except OSError:
            pass
    low = text.lower()
    if "phase 2c/3" in low and "wiggle" in low:
        return "WIGGLE"
    if "edge-candidate-resume" in low:
        return "EDGE"
    if "phase 2a/3" in low:
        return "VERTEX"
    if "phase 1/3" in low:
        return "PREP"
    return "START"


def compact_interesting(text: str, count: int = 4) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines() if any(key in ln for key in INTEREST)]
    out = []
    for line in lines[-count:]:
        out.append(line if len(line) <= 190 else line[:187] + "...")
    return out


def age_string(path: Path) -> str:
    try:
        age = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        return "unknown"
    if age < 120:
        return f"{age}s"
    if age < 7200:
        return f"{age // 60}m"
    return f"{age // 3600}h{(age % 3600) // 60:02d}m"


def foundry_section(campaign: Path) -> list[str]:
    out = ["", "=== EDGE FOUNDRY (cached supervisor state) ==="]
    rows = parse_tsv(campaign / "edge_foundry_job.tsv")
    if rows:
        r = rows[-1]
        out.append(
            "job={job_id} state={state} node={node} cpus={cpus} memGiB={mem_gib} "
            "attempt={attempt} reason={reason}".format(
                job_id=r.get("job_id", "-"), state=r.get("state", "-"),
                node=r.get("node", "-"), cpus=r.get("cpus", "-"),
                mem_gib=r.get("mem_gib", "-"), attempt=r.get("attempt", "-"),
                reason=r.get("reason", "-")
            )
        )
    ready = campaign / "edge_foundry" / "EDGE_READY.tsv"
    failed = campaign / "edge_foundry" / "EDGE_FAILED.tsv"
    if ready.is_file() and ready.stat().st_size:
        out.append("READY: " + ready.read_text(errors="replace").strip())
    elif failed.is_file() and failed.stat().st_size:
        out.append("FAILED: " + failed.read_text(errors="replace").strip())
    else:
        out.append("READY marker: not yet")
    log = campaign / "edge_foundry" / "edge_foundry.log"
    if log.is_file():
        for line in compact_interesting(bounded_tail(log), 5):
            out.append("  " + line)
    return out


def render(campaign: Path) -> str:
    jobs_path = campaign / "jobs.tsv"
    rows = parse_tsv(jobs_path)
    lines = [
        time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        f"RINGARC31 rich dashboard | campaign: {campaign}",
        f"gateway policy: FILE-ONLY renderer; jobs.tsv cache age={age_string(jobs_path)}; no Slurm RPCs from dashboard",
        "",
        "CELL                                      k       tau      state        phase     score        markers      cpus memGiB node",
        "-" * 132,
    ]
    details: list[tuple[str, list[str]]] = []
    for row in rows:
        tag = row.get("tag", "?")
        run_dir = campaign / "runs" / tag
        log = latest_log(campaign, tag)
        text = bounded_tail(log) if log else ""
        state = row.get("state", "-")
        node = row.get("node", "-") or row.get("basis_node", "-") or "-"
        score = score_from_text(text)
        phase = phase_from(run_dir, text)
        markers = marker_summary(run_dir)
        lines.append(
            f"{tag[:40]:<40} {row.get('k','?'):>7} {row.get('tau','?'):>9} "
            f"{state[:12]:<12} {phase:<9} {score:>12} {markers:<12} "
            f"{row.get('cpus','-'):>4} {row.get('mem_gib','-'):>6} {node[:18]}"
        )
        detail = compact_interesting(text, 4)
        if detail:
            details.append((tag, detail))

    lines.extend(foundry_section(campaign))
    lines += ["", "=== LATEST OPERATOR / PHASE EVENTS PER CELL ==="]
    if details:
        for tag, detail in details:
            lines.append(f"[{tag}]")
            lines.extend("  " + item for item in detail)
    else:
        lines.append("No worker events yet.")

    sup = campaign / "logs" / "supervisor.log"
    lines += ["", "=== SUPERVISOR (last 14 lines; cached Slurm monitor) ==="]
    if sup.is_file():
        lines.extend(bounded_tail(sup, 65_536).splitlines()[-14:])
    else:
        lines.append("no supervisor log yet")

    lines += [
        "",
        "Dashboard refresh is visual only: bounded log reads every 10s; no squeue/scontrol.",
        "Supervisor is the sole scheduler poller and normally sleeps ~60s between batched checks.",
        "Detailed cell: tail -f <campaign>/logs/<tag>.log",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign", type=Path)
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS")
    args = ap.parse_args()
    campaign = args.campaign.expanduser().resolve()
    if not campaign.is_dir():
        raise SystemExit(f"campaign does not exist: {campaign}")
    if args.watch:
        seconds = max(10, args.watch)
        try:
            while True:
                sys.stdout.write("\033[2J\033[H" + render(campaign) + "\n")
                sys.stdout.flush()
                time.sleep(seconds)
        except KeyboardInterrupt:
            return 0
    else:
        print(render(campaign))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
