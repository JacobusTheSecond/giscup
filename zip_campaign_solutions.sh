#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
CAMPAIGN=${1:-}
OUT=${2:-}
if [ -z "$CAMPAIGN" ]; then
  echo "usage: $0 CAMPAIGN_DIR [OUTPUT.zip]" >&2
  exit 2
fi
CAMPAIGN=$(realpath -e "$CAMPAIGN")
PY=${PYTHON:-$ENV/bin/python}; [ -x "$PY" ] || PY=python3
if [ -z "$OUT" ]; then OUT="$(dirname "$CAMPAIGN")/$(basename "$CAMPAIGN")_solutions.zip"; fi
OUT=$(realpath -m "$OUT")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/r31-solutions.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
"$PY" "$ROOT/tools/collect_campaign_solutions.py" --campaign "$CAMPAIGN" --output-dir "$TMP/package" >/dev/null
"$PY" - "$TMP/package" "$OUT" <<'PYZIP'
from pathlib import Path
import sys, zipfile
src=Path(sys.argv[1]); out=Path(sys.argv[2]); out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in sorted(src.rglob('*')):
        if p.is_file(): z.write(p, p.relative_to(src))
with zipfile.ZipFile(out) as z:
    bad=z.testzip()
    if bad: raise SystemExit(f'ZIP integrity failure at {bad}')
print(out)
PYZIP
sha256sum "$OUT"
