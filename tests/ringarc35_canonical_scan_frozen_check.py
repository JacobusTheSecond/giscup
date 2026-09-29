#!/usr/bin/env python3
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()
start = SRC.index(
    'constexpr std::size_t max_batch_blocks = 8U;',
    SRC.index('if (use_exact_cached_bounds) {'),
)
end_needle = 'if (!search.deadline_exhausted) evaluate_batch();'
end = SRC.index(end_needle, start) + len(end_needle)
# Ignore indentation introduced by nesting the frozen v35.0.2 loop under an
# explicit canonical branch; every non-whitespace token must remain identical.
normalized = re.sub(r'\s+', '', SRC[start:end])
sha = hashlib.sha256(normalized.encode()).hexdigest()
EXPECTED = '8c15cebb80bc55b2687752e8f82c915f81a415e836cba0a010a47066ca3f80b5'
assert sha == EXPECTED, (sha, EXPECTED)
print('RINGARC35.0.3 canonical cached-scan core frozen from v35.0.2: PASS')
