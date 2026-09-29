#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/main.cpp"
text = SOURCE.read_text()

signature = re.search(
    r"void\s+record\s*\((.*?)\)\s*\n\s*\{",
    text,
    flags=re.DOTALL,
)
assert signature, "OptimizationTimeline::record signature not found"
params = signature.group(1)


def top_level_items(source: str) -> list[str]:
    items: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    matching = {")": "(", "]": "[", "}": "{", ">": "<"}
    in_string = False
    escaped = False
    for index, char in enumerate(source):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in depths:
            depths[char] += 1
        elif char in matching:
            opener = matching[char]
            if depths[opener] > 0:
                depths[opener] -= 1
        elif char == "," and not any(depths.values()):
            items.append(source[start:index].strip())
            start = index + 1
    tail = source[start:].strip()
    if tail:
        items.append(tail)
    return items


parameter_count = len(top_level_items(params))
assert parameter_count == 9, (
    f"OptimizationTimeline::record must accept 9 fields, found {parameter_count}"
)
assert "std::size_t search_round = 0" in params
assert "std::size_t stagnant_cycles = 0" in params
assert (
    "added_candidate,removed_candidate,search_round,stagnant_cycles\\n" in text
), "timeline CSV header does not match record fields"

needle = "timeline->record("
position = 0
calls = 0
while True:
    call = text.find(needle, position)
    if call < 0:
        break
    begin = call + len(needle)
    depth = 1
    index = begin
    in_string = False
    escaped = False
    while index < len(text) and depth:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        index += 1
    assert depth == 0, "unterminated timeline->record call"
    arguments = top_level_items(text[begin:index - 1])
    line = text.count("\n", 0, call) + 1
    assert 5 <= len(arguments) <= parameter_count, (
        f"timeline->record at line {line} supplies {len(arguments)} arguments; "
        f"signature accepts {parameter_count}"
    )
    calls += 1
    position = index

assert calls >= 10, f"unexpectedly found only {calls} timeline calls"
print(f"PASS: {calls} OptimizationTimeline::record calls match the 9-field signature")
