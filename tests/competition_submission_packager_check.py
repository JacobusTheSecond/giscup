#!/usr/bin/env python3
from __future__ import annotations
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="gis-cup-package-") as tmp:
    tmp_path = Path(tmp)
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    for ti, tau in enumerate((0.25, 0.5, 0.75)):
        for ki, k in enumerate((2, 3, 4)):
            coordinates = ",".join(
                f"({i + ti * 0.125:.17g}, {i + ki * 0.25:.17g})"
                for i in range(k)
            )
            (blocks / f"t{ti}_k{ki}.txt").write_text(
                f"({tau:.17g}, {k})\n{coordinates}\nB{ti}{ki}\n",
                encoding="utf-8",
            )
    output = tmp_path / "submission.zip"
    subprocess.run(
        [
            "python3", str(ROOT / "make_competition_submission.py"),
            "--blocks-dir", str(blocks),
            "--source-root", str(ROOT),
            "--output", str(output),
        ],
        check=True,
    )
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert "solutions.txt" in names
        assert "source/src/main.cpp" in names
        assert "source/BUILD_AND_RUN.md" in names
        lines = archive.read("solutions.txt").decode("utf-8").splitlines()
        assert len(lines) == 27
        assert lines[0] == "(0.25, 2)"
        assert lines[-3] == "(0.75, 4)"
print("PASS: official 9-block submission ZIP packager")
