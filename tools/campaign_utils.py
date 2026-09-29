#!/usr/bin/env python3
"""Small campaign utilities shared by the RINGARC31 supervisor.

This module deliberately contains only immutable-executable publication and
startup-marker cleanup.  The older dynamic scheduler carried many historical
RINGARC27 defaults that are not part of the v28 launch path.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil


def file_contains_bytes(path: Path, needle: bytes | str) -> bool:
    if isinstance(needle, str):
        needle = needle.encode("utf-8")
    overlap = max(0, len(needle) - 1)
    tail = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return False
            block = tail + chunk
            if needle in block:
                return True
            tail = block[-overlap:] if overlap else b""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_campaign_executable(
    root: Path,
    campaign_dir: Path,
    expected_revision: str,
) -> tuple[Path, str]:
    source = root / "build/gis_cup_visibility"
    manifest = root / "build/gis_cup_visibility.revision"
    rebuild = "run ./submit_build_v31.sh"
    if not source.is_file() or not os.access(source, os.X_OK):
        raise RuntimeError(
            f"solver executable is missing or not executable: {source}; {rebuild}"
        )
    if not manifest.is_file():
        raise RuntimeError(f"build revision manifest is missing: {manifest}; {rebuild}")

    fields = manifest.read_text().strip().split("\t")
    built_revision = fields[0] if fields else ""
    manifest_sha = fields[1] if len(fields) > 1 else ""
    if built_revision != expected_revision:
        raise RuntimeError(
            f"stale build manifest: expected {expected_revision}, "
            f"found {built_revision or '<empty>'}; {rebuild}"
        )
    if not file_contains_bytes(source, expected_revision):
        raise RuntimeError(
            f"stale executable: embedded revision {expected_revision} was not found; {rebuild}"
        )

    source_sha = sha256_file(source)
    if manifest_sha and source_sha != manifest_sha:
        raise RuntimeError(
            "build executable checksum does not match its revision manifest; "
            "rebuild before submission"
        )

    bin_dir = campaign_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "gis_cup_visibility"
    temporary = bin_dir / f".gis_cup_visibility.tmp.{os.getpid()}"
    shutil.copy2(source, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, target)

    copied_sha = sha256_file(target)
    if copied_sha != source_sha or not file_contains_bytes(target, expected_revision):
        raise RuntimeError("campaign executable snapshot validation failed")

    target_manifest = Path(f"{target}.revision")
    manifest_tmp = Path(f"{target_manifest}.tmp.{os.getpid()}")
    shutil.copy2(manifest, manifest_tmp)
    os.replace(manifest_tmp, target_manifest)

    metadata = campaign_dir / "campaign_binary.tsv"
    metadata_tmp = Path(f"{metadata}.tmp.{os.getpid()}")
    metadata_tmp.write_text(
        "revision\tsha256\tsource\n"
        f"{expected_revision}\t{copied_sha}\t{source}\n"
    )
    os.replace(metadata_tmp, metadata)
    return target, copied_sha


def marker_paths(campaign_dir: Path, tag: str) -> tuple[Path, Path, Path]:
    run_dir = campaign_dir / "runs" / tag
    return (
        run_dir / "startup_begin.tsv",
        run_dir / "startup_ok.tsv",
        run_dir / "startup_failure.tsv",
    )


def clear_markers(campaign_dir: Path, tag: str) -> None:
    for path in marker_paths(campaign_dir, tag):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
