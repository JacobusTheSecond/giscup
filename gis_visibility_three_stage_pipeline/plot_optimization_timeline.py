#!/usr/bin/env python3
"""Plot real/effective GIS visibility scores and optimizer phases over time.

Static mode writes PNG/SVG once. Live mode rereads the timeline CSV and updates
one Matplotlib window at a configurable interval while also refreshing PNG/SVG.
"""

from __future__ import annotations

import argparse
import csv
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
from matplotlib.patches import Patch


@dataclass(frozen=True)
class Row:
    elapsed: float
    event: str
    phase: str
    move_number: int
    cardinality: int
    k: int
    real_threshold: float
    effective_threshold: float
    curve_exponent: float
    real_score: int
    effective_score: int
    covered_boundary: float
    polygon_count: int
    polish_round: int


REQUIRED_COLUMNS = {
    "elapsed_seconds",
    "event",
    "phase",
    "move_number",
    "cardinality",
    "k",
    "real_threshold",
    "effective_threshold",
    "curve_exponent",
    "real_score_fixed_t",
    "effective_score_curve",
    "covered_boundary_m",
    "polygon_count",
    "polish_round",
}


def load_rows(path: Path, *, tolerate_partial: bool = False) -> list[Row]:
    """Load timeline rows.

    In live mode, the optimizer may be in the middle of appending the final CSV
    line. A trailing incomplete line is dropped rather than treated as an error.
    """
    text = path.read_text(encoding="utf-8")
    if tolerate_partial and text and not text.endswith(("\n", "\r")):
        last_newline = max(text.rfind("\n"), text.rfind("\r"))
        text = text[: last_newline + 1] if last_newline >= 0 else ""

    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")

    rows: list[Row] = []
    for raw in reader:
        if tolerate_partial and any(raw.get(column) in (None, "") for column in REQUIRED_COLUMNS):
            continue
        try:
            rows.append(
                Row(
                    elapsed=float(raw["elapsed_seconds"]),
                    event=raw["event"],
                    phase=raw["phase"],
                    move_number=int(raw["move_number"]),
                    cardinality=int(raw["cardinality"]),
                    k=int(raw["k"]),
                    real_threshold=float(raw["real_threshold"]),
                    effective_threshold=float(raw["effective_threshold"]),
                    curve_exponent=float(raw["curve_exponent"]),
                    real_score=int(raw["real_score_fixed_t"]),
                    effective_score=int(raw["effective_score_curve"]),
                    covered_boundary=float(raw["covered_boundary_m"]),
                    polygon_count=int(raw["polygon_count"]),
                    polish_round=int(raw["polish_round"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            if tolerate_partial:
                continue
            raise

    if not rows:
        raise ValueError(f"Timeline is empty: {path}")
    rows.sort(key=lambda row: row.elapsed)
    return rows


def phase_spans(rows: Iterable[Row]) -> list[tuple[float, float, str]]:
    all_rows = list(rows)
    starts: list[tuple[float, str]] = []
    for row in all_rows:
        if row.event != "phase_start":
            continue
        if starts and starts[-1][1] == row.phase:
            continue
        starts.append((row.elapsed, row.phase))

    end_time = max(row.elapsed for row in all_rows)
    if not starts:
        return [(0.0, end_time, "optimization")]

    spans: list[tuple[float, float, str]] = []
    for index, (start, phase) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else end_time
        spans.append((start, max(start, end), phase))
    return spans


def human_phase(phase: str) -> str:
    return {
        "greedy_to_k": "Greedy + 1-swaps to k",
        "greedy_multi_swap_pool": "Greedy-stage restricted pool",
        "greedy_multi_swap": "Greedy-stage restricted multi-swap",
        "greedy_3swap_pool": "Greedy-stage 2/3-swap pool",
        "greedy_3swap": "Greedy-stage restricted 2/3-swap",
        "post_k_pool_build": "Post-k pool construction",
        "post_k_cp_sat": "Post-k CP-SAT (fixed t)",
        "post_k_1swap": "Post-k 1-swaps (fixed t)",
        "complete": "Complete",
    }.get(phase, phase.replace("_", " ").title())


def make_figure() -> tuple[plt.Figure, plt.Axes, plt.Axes]:
    fig, (score_ax, phase_ax) = plt.subplots(
        2,
        1,
        figsize=(13, 7),
        sharex=True,
        gridspec_kw={"height_ratios": [6, 1]},
        constrained_layout=True,
    )
    return fig, score_ax, phase_ax


def draw_plot(
    fig: plt.Figure,
    score_ax: plt.Axes,
    phase_ax: plt.Axes,
    rows: list[Row],
    title: str,
    *,
    live_status: str | None = None,
) -> None:
    score_ax.clear()
    phase_ax.clear()

    times = [row.elapsed for row in rows]
    real_scores = [row.real_score for row in rows]
    effective_scores = [row.effective_score for row in rows]
    spans = phase_spans(rows)
    first = rows[0]
    latest = rows[-1]

    score_ax.step(
        times,
        real_scores,
        where="post",
        linewidth=2.0,
        label=f"Real score: coverage ≥ t = {first.real_threshold:g}",
    )
    score_ax.step(
        times,
        effective_scores,
        where="post",
        linewidth=2.0,
        linestyle="--",
        label=(
            "Effective score: coverage ≥ "
            f"t·(l/k)^m, m = {first.curve_exponent:g}"
        ),
    )
    score_ax.set_ylabel("Qualified polygons")
    upper = max(first.polygon_count, max(real_scores + effective_scores))
    score_ax.set_ylim(bottom=0, top=max(1.0, upper * 1.02))
    score_ax.grid(True, axis="both", alpha=0.25)
    score_ax.legend(loc="best")

    subtitle = (
        f"k={first.k}, polygons={first.polygon_count}; "
        f"latest: {latest.elapsed:.1f}s, l={latest.cardinality}, "
        f"real={latest.real_score}, effective={latest.effective_score}"
    )
    if live_status:
        subtitle += f"; {live_status}"
    score_ax.set_title(f"{title}\n{subtitle}")

    cmap = plt.get_cmap("tab10")
    phase_names: list[str] = []
    for _, _, phase in spans:
        if phase not in phase_names:
            phase_names.append(phase)
    phase_colors = {phase: cmap(index % 10) for index, phase in enumerate(phase_names)}

    total_time = max(times) - min(times)
    label_width = max(1.0, total_time * 0.08)
    for start, end, phase in spans:
        color = phase_colors[phase]
        score_ax.axvspan(start, end, color=color, alpha=0.06, linewidth=0)
        phase_ax.axvspan(start, end, color=color, alpha=0.7, linewidth=0)
        width = end - start
        if width >= label_width:
            phase_ax.text(
                start + width / 2.0,
                0.5,
                human_phase(phase),
                ha="center",
                va="center",
                fontsize=9,
                clip_on=True,
            )

    first_cp_sat = next(
        (
            row.elapsed
            for row in rows
            if row.event == "phase_start" and row.phase in {
                "greedy_multi_swap", "greedy_3swap", "post_k_cp_sat"
            }
        ),
        None,
    )
    if first_cp_sat is not None:
        score_ax.axvline(first_cp_sat, linestyle="--", linewidth=1.2, alpha=0.7)
        score_ax.annotate(
            "First CP-SAT solve",
            xy=(first_cp_sat, score_ax.get_ylim()[1]),
            xytext=(5, -18),
            textcoords="offset points",
            rotation=90,
            va="top",
            fontsize=9,
        )

    phase_ax.set_yticks([])
    phase_ax.set_ylabel("Phase", rotation=0, ha="right", va="center")
    phase_ax.set_xlabel("Optimization elapsed time (seconds)")
    phase_ax.set_ylim(0, 1)
    phase_ax.legend(
        handles=[
            Patch(facecolor=phase_colors[phase], label=human_phase(phase))
            for phase in phase_names
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.55),
        ncol=min(4, max(1, len(phase_names))),
        frameon=False,
    )

    fig.canvas.draw_idle()


def save_figure(fig: plt.Figure, output_png: Path) -> tuple[Path, Path]:
    output_svg = output_png.with_suffix(".svg")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, dpi=180, bbox_inches="tight")
    fig.savefig(output_svg, bbox_inches="tight")
    return output_png, output_svg


def run_static(args: argparse.Namespace) -> None:
    rows = load_rows(args.timeline)
    fig, score_ax, phase_ax = make_figure()
    draw_plot(fig, score_ax, phase_ax, rows, args.title)
    output_png, output_svg = save_figure(fig, args.output)
    print(f"Wrote {output_png}")
    print(f"Wrote {output_svg}")


def run_live(args: argparse.Namespace) -> None:
    if args.interval <= 0.0:
        raise ValueError("--interval must be positive")

    plt.ion()
    fig, score_ax, phase_ax = make_figure()
    fig.canvas.manager.set_window_title("GIS visibility optimization timeline")

    last_signature: tuple[int, int] | None = None
    print(
        f"Watching {args.timeline}; refreshing every {args.interval:g} seconds. "
        "Close the plot window or press Ctrl-C to stop."
    )

    try:
        while plt.fignum_exists(fig.number):
            try:
                stat = args.timeline.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != last_signature:
                    rows = load_rows(args.timeline, tolerate_partial=True)
                    last = rows[-1]
                    status = "complete" if last.event == "finished" else "live"
                    draw_plot(
                        fig,
                        score_ax,
                        phase_ax,
                        rows,
                        args.title,
                        live_status=status,
                    )
                    output_png, output_svg = save_figure(fig, args.output)
                    print(
                        f"Updated at optimizer t={last.elapsed:.1f}s: "
                        f"real={last.real_score}, effective={last.effective_score}, "
                        f"phase={last.phase}; wrote {output_png.name} and {output_svg.name}"
                    )
                    last_signature = signature
            except FileNotFoundError:
                score_ax.clear()
                phase_ax.clear()
                score_ax.text(
                    0.5,
                    0.5,
                    f"Waiting for timeline CSV:\n{args.timeline}",
                    transform=score_ax.transAxes,
                    ha="center",
                    va="center",
                )
                score_ax.set_axis_off()
                phase_ax.set_axis_off()
                fig.canvas.draw_idle()
            except (OSError, ValueError) as exc:
                # A concurrent append may briefly expose an incomplete header or
                # row. Keep the previous good frame and retry next interval.
                print(f"Timeline not ready ({exc}); retrying.")

            fig.canvas.flush_events()
            plt.pause(args.interval)
    except KeyboardInterrupt:
        print("Live plotting stopped.")
    finally:
        plt.ioff()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot fixed-t and curve-threshold scores with optimizer phase timing."
    )
    parser.add_argument("timeline", type=Path, help="optimization_timeline.csv")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="PNG output path; default is next to the CSV",
    )
    parser.add_argument("--title", default="Visibility optimization score over time")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Open a live Matplotlib window and reread the CSV repeatedly",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Live refresh interval in seconds (default 5)",
    )
    args = parser.parse_args()
    args.output = args.output or args.timeline.with_suffix(".png")

    if args.live:
        run_live(args)
    else:
        run_static(args)


if __name__ == "__main__":
    main()
