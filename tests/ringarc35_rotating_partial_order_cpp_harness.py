#!/usr/bin/env python3
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()
start = SRC.index('    auto rotating_partial_block_rank_order = [](')
end = SRC.index('\n\n    struct CachedSwapSearchResult', start)
helper = SRC[start:end].strip()

program = f'''#include <algorithm>\n#include <cstddef>\n#include <iostream>\n#include <vector>\n\nint main() {{\n{helper}\n    auto check = [](const std::vector<std::size_t>& got,\n                    const std::vector<std::size_t>& want) {{\n        if (got != want) return false;\n        auto sorted = got;\n        std::sort(sorted.begin(), sorted.end());\n        for (std::size_t i = 0; i < sorted.size(); ++i) {{\n            if (sorted[i] != i) return false;\n        }}\n        return true;\n    }};\n\n    if (!check(rotating_partial_block_rank_order(10, 2, 5),\n               {{0,1,5,6,7,8,9,2,3,4}})) return 1;\n    if (!check(rotating_partial_block_rank_order(10, 2, 0),\n               {{0,1,2,3,4,5,6,7,8,9}})) return 2;\n    if (!check(rotating_partial_block_rank_order(10, 2, 9),\n               {{0,1,9,2,3,4,5,6,7,8}})) return 3;\n    if (!check(rotating_partial_block_rank_order(3, 4, 2),\n               {{0,1,2}})) return 4;\n    if (!rotating_partial_block_rank_order(0, 4, 0).empty()) return 5;\n    std::cout << "RINGARC35.0.3 rotating block-order harness: PASS\\n";\n    return 0;\n}}\n'''

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    cpp = td / 'test.cpp'
    exe = td / 'test'
    cpp.write_text(program)
    subprocess.run(['g++', '-std=c++20', '-O2', str(cpp), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
