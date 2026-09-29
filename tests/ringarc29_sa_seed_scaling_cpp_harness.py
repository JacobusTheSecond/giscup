#!/usr/bin/env python3
from pathlib import Path
import importlib.util
import os
import sys
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src' / 'main.cpp').read_text()

assert '--exploration-seed-limit must be in [0,8]' in source
assert '--active-sa-seeds must be in [1,8]' in source
assert 'Maximum transient non-improving ruin/crossover seeds (default 2; max 8).' in source
assert 'Split existing SA groups over at most N diverse seeds (default 3; max 8).' in source
assert 'std::array<std::array<double, 3>, 4>' not in source

start = source.index('[[nodiscard]] std::vector<double> sa_seed_allocation_weights(')
end = source.index('\n[[nodiscard]] std::uint64_t scene_geometry_fingerprint', start)
fragment = source[start:end]
program = r'''
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <iostream>
#include <numeric>
#include <stdexcept>
#include <vector>
''' + fragment + r'''
int main() {
    constexpr double expected[4][3] = {
        {0.625, 0.250, 0.125},
        {0.500, 0.3125, 0.1875},
        {0.375, 0.375, 0.250},
        {0.250, 0.4375, 0.3125},
    };
    for (std::size_t level = 0; level < 4; ++level) {
        for (std::size_t n = 1; n <= 8; ++n) {
            const auto w = sa_seed_allocation_weights(n, level);
            if (w.size() != n) return 1;
            double total = 0.0;
            for (double x : w) {
                if (!(x > 0.0) || !std::isfinite(x)) return 2;
                total += x;
            }
            if (std::abs(total - 1.0) > 1e-12) return 3;
            if (n == 3) {
                for (std::size_t i = 0; i < 3; ++i) {
                    if (std::abs(w[i] - expected[level][i]) > 1e-12) return 4;
                }
            }
        }
    }
    bool rejected_zero = false, rejected_nine = false;
    try { (void)sa_seed_allocation_weights(0, 0); } catch (...) { rejected_zero = true; }
    try { (void)sa_seed_allocation_weights(9, 0); } catch (...) { rejected_nine = true; }
    if (!rejected_zero || !rejected_nine) return 5;
    std::cout << "RINGARC29.1 scaled SA seed allocator harness passed\n";
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
    subprocess.run([compiler, '-std=c++20', '-O2', '-Wall', '-Wextra', '-Wpedantic', str(cpp), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)

sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('sup', root / 'submit_v31_supervisor.py')
sup = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules['sup'] = sup
spec.loader.exec_module(sup)
for key in ['GRID_ACTIVE_SA_SEEDS', 'GRID_EXPLORATION_SEED_LIMIT', 'GRID_SA_GROUPS']:
    os.environ.pop(key, None)
expected = {500: (5, 4), 5000: (4, 3), 10000: (3, 2)}
for order, (k, pair) in enumerate(expected.items()):
    task = sup.Task(order=order, k=k, tau=0.5, tag=f't050_k{k}_r00', min_cpus=24, min_mem_mb=65536, max_cpus=48, max_mem_mb=262144)
    values = sup.make_cell_export(task, {}, 48)
    active = int(values['GRID_ACTIVE_SA_SEEDS'])
    transient = int(values['GRID_EXPLORATION_SEED_LIMIT'])
    assert (active, transient) == pair
    assert 1 <= active <= 8
    assert 0 <= transient <= 8
    assert transient <= active - 1
    assert active <= int(values['GRID_SA_GROUPS'])

print('RINGARC29.1 production SA seed presets: PASS')
