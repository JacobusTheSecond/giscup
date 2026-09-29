#!/usr/bin/env python3
"""Lightweight C++ delimiter check that ignores comments and literals."""
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "src/main.cpp"
s = path.read_text()
stack: list[tuple[str, int]] = []
pairs = {')': '(', ']': '[', '}': '{'}
openers = set(pairs.values())
i = 0
line = 1
state = "code"
raw_end = ""
while i < len(s):
    c = s[i]
    n = s[i + 1] if i + 1 < len(s) else ""
    if c == "\n":
        line += 1
    if state == "line_comment":
        if c == "\n": state = "code"
        i += 1; continue
    if state == "block_comment":
        if c == "*" and n == "/": state = "code"; i += 2; continue
        i += 1; continue
    if state in {"string", "char"}:
        if c == "\\": i += 2; continue
        if (state == "string" and c == '"') or (state == "char" and c == "'"):
            state = "code"
        i += 1; continue
    if state == "raw":
        if s.startswith(raw_end, i):
            i += len(raw_end); state = "code"; continue
        i += 1; continue
    if c == "/" and n == "/": state = "line_comment"; i += 2; continue
    if c == "/" and n == "*": state = "block_comment"; i += 2; continue
    if c == 'R' and n == '"':
        j = s.find('(', i + 2)
        if j != -1:
            delimiter = s[i + 2:j]
            raw_end = ')' + delimiter + '"'
            state = "raw"; i = j + 1; continue
    if c == '"': state = "string"; i += 1; continue
    if c == "'": state = "char"; i += 1; continue
    if c in openers:
        stack.append((c, line))
    elif c in pairs:
        assert stack, f"unmatched {c} at line {line}"
        opener, opener_line = stack.pop()
        assert opener == pairs[c], f"{opener} at {opener_line} closed by {c} at {line}"
    i += 1
assert state in {"code", "line_comment"}, f"unterminated lexical state {state}"
assert not stack, f"unclosed delimiters: {stack[-10:]}"
print("PASS: src/main.cpp delimiters balance")
