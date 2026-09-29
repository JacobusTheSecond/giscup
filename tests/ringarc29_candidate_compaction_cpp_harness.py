#!/usr/bin/env python3
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
header = root / 'src' / 'candidate_compaction.hpp'
source = (root / 'src' / 'main.cpp').read_text()

# Source-level guards: filtering is followed by an ID-only compaction, then the
# exact-CGAL engine is built and every surviving row is recomputed.
filter_def = source.index('[[nodiscard]] CandidateLocationFilterStats filter_nearby_edge_candidates(')
filter_end = source.index('\nstruct NormalizedBreakpointStats', filter_def)
compact_pos = source.index('compact_candidate_prefix_verbatim(', filter_def, filter_end)
filter_call = source.index('filter_nearby_edge_candidates(scene, 1.0e-6)')
exact_engine_pos = source.index('exact_engine = std::make_unique<ExactCgalBoundaryVisibilityEngine>', filter_call)
visibility_pos = source.index('collect_visibility_breakpoints(', exact_engine_pos)
assert filter_def < compact_pos < filter_end
assert filter_call < exact_engine_pos < visibility_pos
assert 'const VerifierFT t(sample.canonical_edge_parameter);' in source
assert 'Every surviving visibility row is recomputed from scratch with exact CGAL.' in source

program = r'''
#include <array>
#include <bit>
#include <cstdint>
#include <iostream>
#include <limits>
#include <vector>
#include "candidate_compaction.hpp"

struct Sample {
    std::array<double, 7> geometry{};
    std::uint32_t polygon = 0;
    std::uint32_t segment = 0;
    bool vertex = false;
    bool slide = false;
};

static bool same_double(double a, double b) {
    return std::bit_cast<std::uint64_t>(a) == std::bit_cast<std::uint64_t>(b);
}

static bool same_sample(const Sample& a, const Sample& b) {
    for (std::size_t i = 0; i < a.geometry.size(); ++i) {
        if (!same_double(a.geometry[i], b.geometry[i])) return false;
    }
    return a.polygon == b.polygon && a.segment == b.segment &&
           a.vertex == b.vertex && a.slide == b.slide;
}

int main() {
    const double p = std::nextafter(0.5, 1.0);
    const double q = std::nextafter(0.5, 0.0);
    std::vector<Sample> samples{
        {{{+0.0, -0.0, 1.0, p, 1e-6, -1e-6, 42.25}}, 1, 10, true, false},
        {{{q, p, -0.0, 7.0, 1e-12, 9e12, -3.5}}, 2, 11, false, true},
        {{{-7.25, 3.125, p, q, 1e-300, 1e300, +0.0}}, 3, 12, false, false},
        {{{9.5, -11.75, q, p, -0.0, 2.0, 0.125}}, 4, 13, true, true},
    };
    std::vector<std::uint32_t> candidates{0, 1, 2, 3};
    const Sample expected0 = samples[1];
    const Sample expected1 = samples[3];
    const std::vector<std::uint32_t> survivors{1, 3};

    compact_candidate_prefix_verbatim(samples, candidates, survivors);
    if (samples.size() != 2 || candidates.size() != 2) return 1;
    if (candidates[0] != 0 || candidates[1] != 1) return 2;
    if (!same_sample(samples[0], expected0)) return 3;
    if (!same_sample(samples[1], expected1)) return 4;

    // In particular, signed zero and adjacent binary64 values must survive
    // bit-for-bit: compaction is not allowed to do geometric arithmetic.
    if (!same_double(samples[0].geometry[2], -0.0)) return 5;
    if (!same_double(samples[0].geometry[0], q)) return 6;
    if (!same_double(samples[1].geometry[2], q)) return 7;
    if (!same_double(samples[1].geometry[3], p)) return 8;

    std::cout << "RINGARC29 candidate compaction exactness harness passed\n";
    return 0;
}
'''

compiler = shutil.which('g++') or shutil.which('clang++')
if not compiler:
    raise SystemExit('no C++ compiler found')
with tempfile.TemporaryDirectory() as tmp:
    cpp = Path(tmp) / 'harness.cpp'
    exe = Path(tmp) / 'harness'
    cpp.write_text('#include <cmath>\n' + program)
    subprocess.run([
        compiler, '-std=c++20', '-O2', '-Wall', '-Wextra', '-Wpedantic',
        '-I', str(root / 'src'), str(cpp), '-o', str(exe)
    ], check=True)
    subprocess.run([str(exe)], check=True)
