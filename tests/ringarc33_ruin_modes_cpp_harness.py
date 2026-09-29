#!/usr/bin/env python3
from pathlib import Path
import subprocess, tempfile, textwrap

ROOT = Path(__file__).resolve().parents[1]
src = (ROOT / 'src/main.cpp').read_text()
start_marker = '            // RINGARC33: retain the four proven destroy modes and add five\n'
end_marker = '            // Destruction must happen before we decide which candidates are\n'
a = src.index(start_marker)
b = src.index(end_marker, a)
block = src[a:b]

cpp = r'''
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <numeric>
#include <optional>
#include <random>
#include <span>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

struct Point { double x=0, y=0; };
double squared_distance(Point a, Point b) {
    const double dx=a.x-b.x, dy=a.y-b.y; return dx*dx+dy*dy;
}
struct Polygon { double perimeter=10.0; };
struct Sample { Point boundary_anchor; };
struct Scene {
    std::vector<Polygon> polygons;
    std::vector<Sample> samples;
    std::vector<std::size_t> candidates;
};
std::uint32_t scene_sample_polygon_id(const Scene&, std::uint32_t begin) { return begin; }
struct MockCache {
    std::vector<std::vector<std::uint32_t>> rows;
    template<class F> void for_each_arc(std::size_t c, F f) const {
        for (auto p: rows.at(c)) f(p,p+1U);
    }
};
struct CandidatePolygonMap {
    std::vector<std::vector<std::uint32_t>> rows;
    std::size_t size() const { return rows.size(); }
    std::span<const std::uint32_t> polygons(std::size_t c) const { return rows.at(c); }
};
struct CandidatePolygonReverseIndex {
    std::vector<std::vector<std::uint32_t>> rows;
    std::size_t polygon_count() const { return rows.size(); }
    std::span<const std::uint32_t> candidates(std::size_t p) const { return rows.at(p); }
};
struct Options { double threshold=0.5, near_threshold_floor_ratio=0.85; };
struct RemovalContext { std::vector<std::size_t> default_removal_order; };
struct Result { std::string name; std::vector<std::size_t> order; std::size_t count; };

Result run_mode(std::size_t portfolio_round) {
    Scene scene;
    scene.polygons.resize(20);
    scene.samples.resize(12);
    scene.candidates.resize(12);
    std::iota(scene.candidates.begin(), scene.candidates.end(), 0U);
    for (std::size_t i=0;i<12;++i) scene.samples[i].boundary_anchor = Point{double(i%4)*10.0,double(i/4)*10.0};
    CandidatePolygonMap candidate_polygon_map;
    candidate_polygon_map.rows = {
        {0,1},{0,2},{0,3},{1,4},{2,5},{3,6},{4,7},{5,8},{6,9},{7,10},{8,11},{}
    };
    MockCache cache;
    cache.rows = candidate_polygon_map.rows;
    cache.rows[11] = {0,12}; // non-search duplicate-like selected row: fallback visibility sees polygon 0
    CandidatePolygonReverseIndex candidate_polygon_reverse_index;
    candidate_polygon_reverse_index.rows.resize(20);
    for (std::size_t c=0;c<candidate_polygon_map.rows.size();++c)
        for (auto p:candidate_polygon_map.rows[c]) candidate_polygon_reverse_index.rows[p].push_back((std::uint32_t)c);
    std::vector<std::size_t> selected(12); std::iota(selected.begin(), selected.end(), 0U);
    const std::size_t candidate_count=12;
    std::vector<double> covered(20,5.0); covered[0]=4.9; covered[1]=3.0; covered[2]=3.0;
    Options options;
    RemovalContext removal_context; removal_context.default_removal_order=selected;
    const std::size_t base_ruin=2, minimum_ruin=2, maximum_ruin=6;
    const std::size_t effective_attempts=1, attempt=0;
    std::mt19937_64 rng(0x12345678ULL + portfolio_round);
    auto splitmix64=[](std::uint64_t x)->std::uint64_t { x += 0x9e3779b97f4a7c15ULL; x=(x^(x>>30))*0xbf58476d1ce4e5b9ULL; x=(x^(x>>27))*0x94d049bb133111ebULL; return x^(x>>31); };
    std::vector<std::size_t> removal_order(selected.size()); std::iota(removal_order.begin(), removal_order.end(), 0U);
'''
cpp += block
cpp += r'''
    return {mode_name, removal_order, ruin_count};
}

int main() {
    const std::array<const char*,9> expected = {"low-loss","spatial-cluster","frontier-building","coverage-neighborhood","uniform-random","building-star","stochastic-low-loss","dispersed-random","visibility-graph-cluster"};
    for (std::size_t mode=0; mode<9; ++mode) {
        Result r=run_mode(mode);
        if (r.name != expected[mode]) { std::cerr << "mode name mismatch " << mode << ": " << r.name << "\n"; return 2; }
        if (r.count < 2 || r.count > 6 || r.order.size() != r.count) { std::cerr << "bad count/order for " << r.name << "\n"; return 3; }
        auto copy=r.order; std::sort(copy.begin(),copy.end());
        if (std::adjacent_find(copy.begin(),copy.end()) != copy.end()) { std::cerr << "duplicate slot in " << r.name << "\n"; return 4; }
        for (auto slot:copy) if (slot>=12) { std::cerr << "invalid slot\n"; return 5; }
        if (mode==5 && r.count != 4) { std::cerr << "building-star should include 3 CSR guards + duplicate fallback = 4, got " << r.count << "\n"; return 6; }
        std::cout << mode << " " << r.name << " count=" << r.count << "\n";
    }
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix='r33-ruin-harness-') as td:
    td=Path(td); source=td/'harness.cpp'; exe=td/'harness'; source.write_text(cpp)
    compiler='/usr/bin/c++'
    subprocess.run([compiler,'-std=c++20','-O2','-Wall','-Wextra','-Wpedantic',str(source),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
print('RINGARC33 extracted ruin-mode C++ harness: PASS')
