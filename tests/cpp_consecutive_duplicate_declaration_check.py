#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else 'src/main.cpp')
text = path.read_text()
# This catches the exact class of compile blocker found during the second audit:
# two consecutive declarations of the same local name and type. It deliberately
# avoids trying to be a complete C++ parser.
pattern = re.compile(
    r'(?m)^([ \t]*)(?!else\b|if\b|for\b|while\b|return\b|switch\b|case\b)'
    r'(const[ \t]+)?((?:std::)?[A-Za-z_]\w*(?:::\w+)*(?:<[^;\n]+>)?)'
    r'[ \t]+([A-Za-z_]\w*)[ \t]*(?:=|\{|\()[^;\n]*;[ \t]*\n'
    r'\1(?:const[ \t]+)?\3[ \t]+\4[ \t]*(?:=|\{|\()[^;\n]*;'
)
matches = list(pattern.finditer(text))
if matches:
    for match in matches:
        line = text.count('\n', 0, match.start()) + 1
        print(f'consecutive duplicate declaration near line {line}: {match.group(4)}')
    raise SystemExit(1)
print(f'PASS: no consecutive duplicate declarations in {path}')
