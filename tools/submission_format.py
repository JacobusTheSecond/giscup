#!/usr/bin/env python3
"""Validate nine GIS Cup solution blocks and build the official submission ZIP."""
from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

FLOAT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
PAIR_RE = re.compile(rf"^\(\s*({FLOAT})\s*,\s*(\d+)\s*\)$")
COORD_RE = re.compile(rf"\(\s*({FLOAT})\s*,\s*({FLOAT})\s*\)")

EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", "build", "logs", "output", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXCLUDED_SUFFIXES = {".o", ".obj", ".a", ".so", ".dylib", ".pyc", ".zip"}


class SubmissionError(ValueError):
    pass


def parse_coordinate_line(line: str, expected_k: int, path: Path) -> None:
    cursor = 0
    coordinates = 0
    for match in COORD_RE.finditer(line):
        separator = line[cursor:match.start()]
        if coordinates == 0:
            if separator.strip():
                raise SubmissionError(f"{path}: unexpected text before first coordinate")
        elif separator.strip() != ",":
            raise SubmissionError(f"{path}: coordinates must be comma-separated")
        x = float(match.group(1))
        y = float(match.group(2))
        if not math.isfinite(x) or not math.isfinite(y):
            raise SubmissionError(f"{path}: coordinates must be finite IEEE-754 doubles")
        coordinates += 1
        cursor = match.end()
    if line[cursor:].strip():
        raise SubmissionError(f"{path}: unexpected text after final coordinate")
    if coordinates != expected_k:
        raise SubmissionError(
            f"{path}: line 2 contains {coordinates} coordinates; expected k={expected_k}"
        )


def parse_block(path: Path) -> tuple[float, int, tuple[str, str, str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubmissionError(f"cannot read {path}: {exc}") from exc
    lines = raw.splitlines()
    if len(lines) != 3:
        raise SubmissionError(f"{path}: expected exactly three lines, found {len(lines)}")
    pair = PAIR_RE.fullmatch(lines[0].strip())
    if pair is None:
        raise SubmissionError(f"{path}: line 1 must be '(tau, k)'")
    tau = float(pair.group(1))
    k = int(pair.group(2))
    if not math.isfinite(tau) or not (0.0 < tau <= 1.0):
        raise SubmissionError(f"{path}: tau must be finite and in (0, 1]")
    if k <= 0:
        raise SubmissionError(f"{path}: k must be positive")
    parse_coordinate_line(lines[1], k, path)
    # Building IDs are intentionally preserved verbatim. An empty line is valid
    # when no building is claimed; commas delimit IDs otherwise.
    if "\x00" in lines[2]:
        raise SubmissionError(f"{path}: building-ID line contains a NUL byte")
    return tau, k, (lines[0], lines[1], lines[2])


def source_files(root: Path, output: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        try:
            if path.resolve() == output.resolve():
                continue
        except OSError:
            pass
        yield path, relative


def gather_blocks(args: argparse.Namespace) -> list[Path]:
    paths = [Path(item) for item in args.blocks]
    if args.blocks_dir is not None:
        directory = Path(args.blocks_dir)
        if not directory.is_dir():
            raise SubmissionError(f"block directory does not exist: {directory}")
        paths.extend(sorted(directory.glob(args.block_glob)))
    # Resolve duplicate command-line paths without changing deterministic order.
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def build_submission(args: argparse.Namespace) -> Path:
    block_paths = gather_blocks(args)
    if len(block_paths) != 9:
        raise SubmissionError(
            f"expected exactly nine solution blocks, found {len(block_paths)}"
        )

    parsed = [parse_block(path) for path in block_paths]
    by_pair: dict[tuple[float, int], tuple[str, str, str]] = {}
    for tau, k, lines in parsed:
        key = (tau, k)
        if key in by_pair:
            raise SubmissionError(f"duplicate parameter pair: ({tau}, {k})")
        by_pair[key] = lines

    taus = sorted({tau for tau, _ in by_pair})
    ks = sorted({k for _, k in by_pair})
    expected_pairs = {(tau, k) for tau in taus for k in ks}
    if len(taus) != 3 or len(ks) != 3 or set(by_pair) != expected_pairs:
        raise SubmissionError(
            "the nine blocks must form a complete 3-threshold × 3-k Cartesian grid"
        )

    output = Path(args.output).resolve()
    source_root = Path(args.source_root).resolve()
    if not source_root.is_dir():
        raise SubmissionError(f"source root does not exist: {source_root}")
    output.parent.mkdir(parents=True, exist_ok=True)

    ordered_lines: list[str] = []
    for tau in taus:
        for k in ks:
            ordered_lines.extend(by_pair[(tau, k)])
    solutions_text = "\n".join(ordered_lines) + "\n"

    with tempfile.TemporaryDirectory(prefix="gis-cup-submission-") as temp_dir:
        temp = Path(temp_dir)
        solutions_path = temp / args.solutions_name
        solutions_path.write_text(solutions_text, encoding="utf-8", newline="\n")

        temporary_zip = temp / "submission.zip"
        with zipfile.ZipFile(
            temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.write(solutions_path, args.solutions_name)
            for path, relative in source_files(source_root, output):
                archive.write(path, Path(args.source_folder) / relative)
        shutil.copy2(temporary_zip, output)

    with zipfile.ZipFile(output, "r") as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise SubmissionError(f"ZIP integrity check failed at {corrupt}")
        names = set(archive.namelist())
        if args.solutions_name not in names:
            raise SubmissionError("ZIP is missing the solutions text file")
        prefix = args.source_folder.rstrip("/") + "/"
        if not any(name.startswith(prefix) for name in names):
            raise SubmissionError("ZIP is missing the source-code folder")

    return output


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the official GIS Cup ZIP from nine solver output blocks."
    )
    parser.add_argument("blocks", nargs="*", help="three-line solution block files")
    parser.add_argument("--blocks-dir", help="directory containing solution blocks")
    parser.add_argument("--block-glob", default="*.txt", help="glob used with --blocks-dir")
    parser.add_argument("--source-root", default=Path(__file__).resolve().parent)
    parser.add_argument("--output", required=True, help="output submission ZIP")
    parser.add_argument("--solutions-name", default="solutions.txt")
    parser.add_argument("--source-folder", default="source")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        output = build_submission(args)
    except SubmissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Submission ZIP written and verified: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
