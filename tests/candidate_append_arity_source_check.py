#!/usr/bin/env python3
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "src/main.cpp").read_text()


def extract_balanced(text: str, start: int, open_ch: str, close_ch: str) -> tuple[str, int]:
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1], i + 1
    raise AssertionError(f"unbalanced {open_ch}{close_ch}")


def top_level_arg_count(call: str) -> int:
    inner = call[call.index("(") + 1 : call.rindex(")")]
    if not inner.strip():
        return 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    count = 1
    in_string = False
    quote = ""
    escaped = False
    for ch in inner:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote = ch
            continue
        if ch in depths:
            depths[ch] += 1
        elif ch in pairs and depths[pairs[ch]] > 0:
            depths[pairs[ch]] -= 1
        elif ch == "," and all(v == 0 for v in depths.values()):
            count += 1
    return count


# The dynamic BoundaryPoint propagation overload deliberately has a compact
# five-argument append helper. CandidateVertex's two newer provenance members
# are default-initialized in this path; passing the seven-argument call shape
# from the index-based overload is a compile error.
marker = """    [[nodiscard]] std::vector<CandidateVertex> compute_visibility_candidates(\n        const BoundaryPoint& query_sample,"""
start = SRC.index(marker)
body_start = SRC.index("{", start)
body, _ = extract_balanced(SRC, body_start, "{", "}")

lambda_marker = "auto append = [&]"
lambda_start = body.index(lambda_marker)
params_start = body.index("(", lambda_start)
params, _ = extract_balanced(body, params_start, "(", ")")
assert top_level_arg_count("f" + params) == 5, params

calls = []
pos = 0
while True:
    pos = body.find("append(", pos)
    if pos < 0:
        break
    call, end = extract_balanced(body, body.index("(", pos), "(", ")")
    calls.append("append" + call)
    pos = end

assert len(calls) == 4, calls
for call in calls:
    assert top_level_arg_count(call) == 5, call

# Keep the primary candidate-generation overload provenance-aware.
primary_marker = """    [[nodiscard]] std::vector<CandidateVertex> compute_visibility_candidates(\n        std::size_t candidate_index,"""
primary_start = SRC.index(primary_marker)
primary_body_start = SRC.index("{", primary_start)
primary_body, _ = extract_balanced(SRC, primary_body_start, "{", "}")
assert "bool collinear_incident" in primary_body
assert "std::uint32_t provenance_incident" in primary_body
assert "collinear_incident_provenance" in SRC

print("RINGARC28.3.1 candidate append-arity regression: PASS")
