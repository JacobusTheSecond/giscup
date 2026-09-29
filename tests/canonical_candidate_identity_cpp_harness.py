#!/usr/bin/env python3
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src' / 'main.cpp').read_text()
start = source.index('class CanonicalCandidateIdentitySet')
end = source.index('\nstruct CandidateSubdivisionStats', start)
block = source[start:end]

program = r'''
#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

struct Point { double px = 0.0; double py = 0.0; };
double x(const Point& p) { return p.px; }
double y(const Point& p) { return p.py; }
struct BoundaryPoint { Point point; Point boundary_anchor; };
''' + block + r'''

int main() {
    std::vector<BoundaryPoint> samples;
    samples.reserve(1000000);
    CanonicalCandidateIdentitySet set(samples, 2);

    auto insert = [&](std::uint32_t sid, bool slide, Point point) {
        if (samples.size() >= std::numeric_limits<std::uint32_t>::max()) {
            throw std::runtime_error("sample overflow");
        }
        const auto sample_id = static_cast<std::uint32_t>(samples.size());
        const bool unique = set.insert(sid, slide, point, sample_id);
        if (unique) samples.push_back({point, point});
        return unique;
    };

    std::size_t unique = 0;
    for (std::uint32_t sid = 0; sid < 2000; ++sid) {
        for (std::uint32_t i = 0; i < 200; ++i) {
            const Point p{static_cast<double>(i) / 199.0,
                          static_cast<double>(sid) * 0.25};
            unique += insert(sid, false, p) ? 1 : 0;
            if (insert(sid, false, p)) return 1;
            unique += insert(sid, true, p) ? 1 : 0;
            if (insert(sid, true, p)) return 2;
        }
    }
    const std::size_t expected = 2000ULL * 200ULL * 2ULL;
    if (unique != expected || set.size() != expected) return 3;
    if (!insert(999999, false, Point{-0.0, 0.0})) return 4;
    if (insert(999999, false, Point{0.0, -0.0})) return 5;
    if (set.allocated_bytes() == 0 || set.allocated_bytes() % 16 != 0) return 6;
    set.release();
    if (set.size() != 0 || set.allocated_bytes() != 0) return 7;
    std::cout << "canonical candidate identity harness passed\n";
    return 0;
}
'''

compiler = shutil.which('g++') or shutil.which('clang++')
if not compiler:
    raise SystemExit('no C++ compiler found')
with tempfile.TemporaryDirectory() as tmp:
    cpp = Path(tmp) / 'harness.cpp'
    exe = Path(tmp) / 'harness'
    cpp.write_text(program)
    subprocess.run([compiler, '-std=c++20', '-O2', str(cpp), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
