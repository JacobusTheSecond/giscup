#!/usr/bin/env python3
from pathlib import Path

source = (Path(__file__).resolve().parents[1] / "src" / "main.cpp").read_text()

stage1 = source.index("// Stage 1: compute one seed visibility polygon")
stage2 = source.index("// Stage 2: convert the vertices of the seed visibility polygons")
stage3 = source.index("// Stage 3: compute a visibility polygon for every final boundary guard")
assert stage1 < stage2 < stage3

main_tail = source[stage3:]
end = main_tail.index("const double geometry_seconds")
stage3_block = main_tail[:end]
assert "const std::vector<std::uint32_t> final_candidate_ids = scene.candidates" in stage3_block
assert "collect_visibility_breakpoints" in stage3_block
assert "append_coverage_samples_from_breakpoints" in stage3_block
assert "validate_coverage_only_append" in stage3_block
assert "append_candidate_sample" not in stage3_block

collect_start = source.index("[[nodiscard]] SegmentBreakpoints collect_visibility_breakpoints")
collect_end = source.index("void encode_varint", collect_start)
collect_body = source[collect_start:collect_end]
assert "const Scene& scene" in collect_body
assert "scene.candidates.push_back" not in collect_body
assert "append_candidate_sample" not in collect_body

assert 'PROGRAM_REVISION = "PIPELINE1"' in source
assert 'algorithm_revision = 0x50495045303031ULL' in source
assert "zero recursive candidates" in source

print("PASS: explicit three-stage pipeline; Stage 3 cannot append recursive candidates")
