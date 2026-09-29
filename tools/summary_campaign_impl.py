#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_reports(campaign: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted((campaign / "runs").glob("*/conclusion_report.json")):
        try:
            report = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            reports.append({"_path": str(path), "_error": str(exc)})
            continue
        report["_path"] = str(path)
        reports.append(report)
    return reports


def verified_score(report: dict[str, Any]) -> int:
    value = report.get("verification", {}).get("real_score")
    return int(value) if isinstance(value, int) else -1


def solver_score(report: dict[str, Any]) -> int:
    verified = verified_score(report)
    if verified >= 0:
        return verified
    value = report.get("timeline", {}).get("final_score")
    return int(value) if isinstance(value, int) else -1


def search_complete(report: dict[str, Any]) -> bool:
    meta = report.get("metadata", {})
    return bool(
        meta.get("search_completed") is True
        or meta.get("status") in {"complete", "complete_verified", "complete_unverified"}
    ) and solver_score(report) >= 0


def independently_verified(report: dict[str, Any]) -> bool:
    if not (
        report.get("verification", {}).get("feasible") == "yes"
        and report.get("verification", {}).get("gate_marker_present") is True
        and verified_score(report) >= 0
    ):
        return False
    try:
        run_dir = Path(report["_path"]).parent
        marker_path = run_dir / "VERIFIED_OK.tsv"
        marker_before = marker_path.read_bytes()
        marker_fields = marker_before.decode("utf-8").strip().split("\t")
        if len(marker_fields) != 6:
            return False
        revision, k_text, tau_text, score_text, expected_sha, _ = marker_fields
        meta = report.get("metadata", {})
        # Authenticate against the revision recorded by that historical run so
        # this post-hoc reporter can safely regenerate 29.1 as well as v30.
        expected_revision = str(meta.get("revision") or (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip())
        if not expected_revision or revision != expected_revision:
            return False
        if int(k_text) != int(meta.get("k")):
            return False
        if abs(float(tau_text) - float(meta.get("tau"))) > 1e-12:
            return False
        if int(score_text) != verified_score(report):
            return False
        submission = Path(meta["submission_block"])
        block_bytes = submission.read_bytes()
        marker_after = marker_path.read_bytes()
        if marker_before != marker_after:
            return False
        if hashlib.sha256(block_bytes).hexdigest() != expected_sha:
            return False
        block_lines = block_bytes.decode("utf-8").splitlines()
        if len(block_lines) != 3:
            return False
        report["_verified_block_text"] = "\n".join(block_lines)
        report["_verified_block_sha256"] = expected_sha
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False
    return True

def key(report: dict[str, Any]) -> tuple[int, float]:
    meta = report.get("metadata", {})
    return int(meta.get("k") or -1), float(meta.get("tau") or -1.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True, type=Path)
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    reports = load_reports(campaign)

    grouped: dict[tuple[int, float], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        if "_error" not in report:
            grouped[key(report)].append(report)

    best: dict[tuple[int, float], dict[str, Any]] = {}
    for cell, candidates in grouped.items():
        completed = [report for report in candidates if search_complete(report)]
        if completed:
            best[cell] = max(
                completed,
                key=lambda report: (
                    solver_score(report),
                    report.get("timeline", {}).get("final_covered_boundary_m") or 0.0,
                    -(report.get("metadata", {}).get("replica") or 0),
                ),
            )

    foundry_dir = campaign / "edge_foundry"
    foundry_ready = foundry_dir / "EDGE_READY.tsv"
    foundry_resource = foundry_dir / "resource_foundry.txt"
    foundry_log = foundry_dir / "edge_foundry.log"
    foundry = {
        "ready": foundry_ready.is_file(),
        "manifest": foundry_ready.read_text(errors="replace").strip() if foundry_ready.is_file() else None,
        "resource_present": foundry_resource.is_file(),
        "log_present": foundry_log.is_file(),
    }

    json_output = {
        "schema": "ringarc30.campaign-summary.v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": str(campaign),
        "reports_found": len(reports),
        "edge_foundry": foundry,
        "cells": [],
    }
    lines = [
        "# RINGARC31 campaign summary",
        "",
        f"Campaign: `{campaign}`",
        f"Reports found: `{len(reports)}`",
        f"Exact edge foundry ready: `{foundry['ready']}`; manifest: `{foundry['manifest'] or 'missing'}`",
        "",
        "| k | tau | completed replicas | best solver score | independent verify | replica/profile | SA seed | report |",
        "|---:|---:|---:|---:|---|---|---:|---|",
    ]
    blocks: list[str] = []
    block_records: list[tuple[float, int, str]] = []
    expected_cells: list[tuple[int, float]] = []
    jobs_path = campaign / "jobs.tsv"
    if jobs_path.is_file():
        for line in jobs_path.read_text(errors="replace").splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) >= 4:
                try:
                    expected_cells.append((int(fields[2]), float(fields[3])))
                except ValueError:
                    pass
    if not expected_cells:
        expected_cells = sorted(grouped, key=lambda item: (-item[0], -item[1]))
    expected_cells = list(dict.fromkeys(expected_cells))

    for k, tau in expected_cells:
        cell = (k, tau)
        candidates = grouped.get(cell, [])
        completed = [report for report in candidates if search_complete(report)]
        winner = best.get(cell)
        if winner is None:
            lines.append(f"| {k} | {tau:.6g} | {len(completed)}/{len(candidates)} | n/a | no | n/a | n/a | n/a |")
            json_output["cells"].append({
                "k": k, "tau": tau, "replicas_found": len(candidates),
                "replicas_complete": len(completed), "best": None,
            })
            continue
        meta = winner["metadata"]
        score = solver_score(winner)
        verified = independently_verified(winner)
        report_path = winner["_path"]
        lines.append(
            f"| {k} | {tau:.6g} | {len(completed)}/{len(candidates)} | {score} | "
            f"{'yes' if verified else 'no'} | {meta.get('replica')} / {meta.get('profile')} | "
            f"{meta.get('sa_seed')} | `{report_path}` |"
        )
        block_text = winner.get("_verified_block_text")
        if isinstance(block_text, str):
            blocks.append(block_text)
            block_records.append((tau, k, block_text))
        json_output["cells"].append({
            "k": k, "tau": tau, "replicas_found": len(candidates),
            "replicas_complete": len(completed),
            "best": {
                "score": score,
                "independently_verified": verified,
                "replica": meta.get("replica"), "profile": meta.get("profile"),
                "sa_seed": meta.get("sa_seed"), "ruin_seed": meta.get("ruin_seed"),
                "report": report_path, "submission_block": meta.get("submission_block"),
            },
        })

    lines.extend([
        "",
        "## Cross-run observations",
        "",
    ])
    sa_wins = 0
    ruin_wins = 0
    crossover_wins = 0
    edge_cells = 0
    repair_full_scans = 0
    repair_partial_scans = 0
    repair_partial_salvaged = 0
    repair_admission_skips = 0
    repair_eval = 0
    repair_pruned = 0
    repair_max_scan = 0.0
    repair_max_predicted = 0.0
    sa_phase_max = 0.0
    post_k_sa_max = 0.0
    operator_totals: dict[str, dict[str, float]] = defaultdict(lambda: {
        "attempts": 0.0, "improvements": 0.0, "score_gain": 0.0, "near_gain": 0.0
    })
    for report in best.values():
        log = report.get("log", {})
        sa_wins += int(log.get("sa_improvements") or 0)
        ruin_wins += int(log.get("ruin_improvements") or 0)
        crossover_wins += int(log.get("crossover_improvements") or 0)
        edge_cells += int(bool(log.get("edge_pool_accepted")))
        repair_full_scans += int(log.get("repair_full_scans") or 0)
        repair_partial_scans += int(log.get("repair_partial_scans") or 0)
        repair_partial_salvaged += int(log.get("repair_partial_salvaged") or 0)
        repair_admission_skips += int(log.get("repair_admission_skips") or 0)
        repair_eval += int(log.get("repair_evaluated_candidates") or 0)
        repair_pruned += int(log.get("repair_cached_pruned") or 0)
        repair_max_scan = max(repair_max_scan, float(log.get("repair_max_scan_seconds") or 0.0))
        repair_max_predicted = max(repair_max_predicted, float(log.get("repair_max_predicted_scan_seconds") or 0.0))
        phase_values = log.get("sa_phase_seconds") or []
        if phase_values:
            sa_phase_max = max(sa_phase_max, max(float(x) for x in phase_values))
        post_values = log.get("post_k_sa_assigned_seconds") or []
        if post_values:
            post_k_sa_max = max(post_k_sa_max, max(float(x) for x in post_values))
        for name, stats in (log.get("operator_stats") or {}).items():
            total = operator_totals[name]
            for field in ("attempts", "improvements", "score_gain", "near_gain"):
                total[field] += float(stats.get(field) or 0.0)
    lines.append(f"- Winning cells that consumed the exact edge pool: `{edge_cells}/{len(best)}`")
    lines.append(f"- Accepted SA incumbent improvements across winning cells: `{sa_wins}`")
    lines.append(f"- Accepted ruin/recreate incumbent improvements across winning cells: `{ruin_wins}`")
    lines.append(f"- Accepted crossover incumbent improvements across winning cells: `{crossover_wins}`")
    lines.append(f"- SA horizon evidence: max observed phase `{sa_phase_max:.1f}s`; max post-k assigned slice `{post_k_sa_max:.1f}s`.")
    lines.append(f"- Live repair: full scans `{repair_full_scans}`, partial scans `{repair_partial_scans}`, salvaged partial swaps `{repair_partial_salvaged}`, admission skips `{repair_admission_skips}`.")
    lines.append(f"- Live repair work: exact-evaluated `{repair_eval}`, cached-pruned `{repair_pruned}`, max actual scan `{repair_max_scan:.3f}s`, max predicted scan `{repair_max_predicted:.3f}s`.")
    if operator_totals:
        lines.append(f"- Aggregated operator evidence: `{json.dumps(operator_totals, sort_keys=True)}`")
    lines.append("- Feed this file together with the most interesting per-run `conclusion_report.md` files into the next tuning iteration.")
    json_output["aggregate_operator_evidence"] = operator_totals
    json_output["aggregate_sa_repair_evidence"] = {
        "max_sa_phase_seconds": sa_phase_max,
        "max_post_k_sa_assigned_seconds": post_k_sa_max,
        "repair_full_scans": repair_full_scans,
        "repair_partial_scans": repair_partial_scans,
        "repair_partial_salvaged": repair_partial_salvaged,
        "repair_admission_skips": repair_admission_skips,
        "repair_evaluated_candidates": repair_eval,
        "repair_cached_pruned": repair_pruned,
        "repair_max_scan_seconds": repair_max_scan,
        "repair_max_predicted_scan_seconds": repair_max_predicted,
    }

    package_status: dict[str, Any] = {
        "complete_verified_grid": len(block_records) == 9,
        "submission_zip": None,
        "error": None,
    }
    if blocks:
        (campaign / "best_submission_blocks.txt").write_text(
            "\n".join(blocks) + "\n"
        )
    if len(block_records) == 9:
        verified_dir = campaign / "verified_solution_blocks"
        if verified_dir.exists():
            shutil.rmtree(verified_dir)
        verified_dir.mkdir(parents=True)
        block_paths: list[Path] = []
        for tau, k, block_text in sorted(block_records):
            block_path = verified_dir / f"tau_{tau:.17g}_k_{k}.txt"
            block_path.write_text(block_text + "\n", encoding="utf-8")
            block_paths.append(block_path)
        submission_zip = campaign / "gis_cup_submission_verified.zip"
        command = [
            sys.executable,
            str(Path(__file__).resolve().parent / "make_competition_submission.py"),
            *[str(path) for path in block_paths],
            "--source-root", str(Path(__file__).resolve().parent),
            "--output", str(submission_zip),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and submission_zip.is_file():
            package_status["submission_zip"] = str(submission_zip)
            lines.append(
                f"- Official verified submission ZIP: `{submission_zip}`"
            )
        else:
            package_status["error"] = completed.stdout.strip()
            lines.append(
                "- Verified blocks were complete, but automatic ZIP packaging failed; "
                "inspect `campaign_report.json`."
            )
    else:
        lines.append(
            f"- Official ZIP not produced: only `{len(block_records)}/9` independently verified cells were available."
        )
    json_output["official_package"] = package_status

    (campaign / "campaign_report.json").write_text(
        json.dumps(json_output, indent=2, sort_keys=True) + "\n"
    )
    (campaign / "campaign_report.md").write_text("\n".join(lines) + "\n")

    print("===== BEGIN RINGARC31 CAMPAIGN FEEDBACK PACKET =====")
    print("\n".join(lines))
    print("===== END RINGARC31 CAMPAIGN FEEDBACK PACKET =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
