#!/usr/bin/env python3
"""Compile and run the exact RINGARC11 arc add/1-swap block without GIS dependencies."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "main.cpp").read_text()
START = SOURCE.index("struct ReplacementNeighborhoodEvaluation {")
END = SOURCE.index("[[nodiscard]] std::vector<std::size_t> build_polish_pool(", START)
BLOCK = SOURCE[START:END]

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <span>
#include <utility>
#include <vector>

struct BoundaryPoint {
    std::uint32_t polygon_id = 0;
    std::uint32_t ring_id = 0;
    double weight = 0.0;
};
struct PolygonData { double perimeter = 0.0; };
struct Scene {
    std::vector<PolygonData> polygons;
    std::vector<BoundaryPoint> samples;
    std::vector<std::uint32_t> candidates;
    std::vector<double> sample_weight_prefix;
};
std::size_t scene_sample_count(const Scene& scene) { return scene.samples.size(); }
bool scene_has_weight_index(const Scene& scene) {
    return scene.sample_weight_prefix.size() == scene.samples.size();
}
std::uint32_t scene_sample_polygon_id(const Scene& scene, std::uint32_t id) {
    return scene.samples.at(id).polygon_id;
}
std::uint32_t scene_sample_ring_id(const Scene& scene, std::uint32_t id) {
    return scene.samples.at(id).ring_id;
}
class VisibilityCache {
public:
    std::vector<std::vector<std::pair<std::uint32_t, std::uint32_t>>> rows;
    template<class Fn> void for_each_arc(std::size_t candidate, Fn&& fn) const {
        for (const auto& [begin, length] : rows.at(candidate)) fn(begin, length);
    }
    bool is_search_candidate(std::size_t candidate) const { return candidate < rows.size(); }
};
struct CandidatePolygonMap { std::vector<std::vector<std::uint32_t>> polygons_by_candidate; };
bool candidate_is_hot(
    std::size_t, const CandidatePolygonMap&,
    const std::vector<std::uint64_t>&,
    const std::vector<std::uint64_t>&) { return true; }
double sum_covered(const std::vector<double>& covered) {
    return std::accumulate(covered.begin(), covered.end(), 0.0);
}
double near_threshold_contribution(
    double covered, double required, double floor_ratio)
{
    if (!(required > 0.0)) return 1.0;
    if (covered >= required) return 1.0;
    const double floor = std::clamp(floor_ratio, 0.0, 0.999999) * required;
    if (covered <= floor) return 0.0;
    const double x = std::clamp((covered - floor) / (required - floor), 0.0, 1.0);
    return 0.05 + 0.95 * x * x;
}

double near_threshold_progress(
    const Scene& scene,
    const std::vector<double>& covered,
    double threshold,
    double floor_ratio)
{
    if (scene.polygons.empty()) return 0.0;
    double progress = 0.0;
    for (std::size_t polygon = 0; polygon < scene.polygons.size(); ++polygon) {
        progress += near_threshold_contribution(
            covered[polygon], threshold * scene.polygons[polygon].perimeter,
            floor_ratio);
    }
    return progress / static_cast<double>(scene.polygons.size());
}

int solution_score(
    const Scene& scene,
    const std::vector<double>& covered,
    double threshold)
{
    int score = 0;
    for (std::size_t polygon_id = 0; polygon_id < scene.polygons.size(); ++polygon_id) {
        if (covered[polygon_id] + 1e-10 >=
            threshold * scene.polygons[polygon_id].perimeter) {
            ++score;
        }
    }
    return score;
}
bool coverage_qualified(double visible, double target) {
    return visible + 1e-10 >= target;
}
struct SearchQuality {
    int score = 0;
    double near_progress = 0.0;
    double total = 0.0;
};
SearchQuality evaluate_search_quality(
    const Scene& scene,
    const std::vector<double>& covered,
    double threshold,
    double near_floor)
{
    return {
        solution_score(scene, covered, threshold),
        near_threshold_progress(scene, covered, threshold, near_floor),
        sum_covered(covered)};
}
double sample_range_weight(const Scene& scene, std::uint32_t begin, std::uint32_t end_exclusive) {
    if (begin > end_exclusive || static_cast<std::size_t>(end_exclusive) > scene.sample_weight_prefix.size()) {
        throw std::runtime_error("bad prefix query");
    }
    if (begin == end_exclusive) return 0.0;
    const BoundaryPoint& first = scene.samples.at(begin);
    const BoundaryPoint& last = scene.samples.at(end_exclusive - 1U);
    if (first.polygon_id != last.polygon_id || first.ring_id != last.ring_id) throw std::runtime_error("cross-ring");
    double weight = scene.sample_weight_prefix[end_exclusive - 1U];
    if (begin > 0) {
        const BoundaryPoint& prior = scene.samples[begin - 1U];
        if (prior.polygon_id == first.polygon_id && prior.ring_id == first.ring_id) weight -= scene.sample_weight_prefix[begin - 1U];
    }
    return weight;
}
'''

POSTLUDE = r'''
struct ReferenceResult { int score; double total; std::size_t slot; int crossings; double rescue; double novel; };

std::vector<std::uint32_t> expand(const VisibilityCache& cache, std::size_t candidate) {
    std::vector<std::uint32_t> out;
    cache.for_each_arc(candidate, [&](std::uint32_t begin, std::uint32_t length) {
        for (std::uint32_t offset = 0; offset < length; ++offset) out.push_back(begin + offset);
    });
    return out;
}

struct ReferenceAddResult {
    std::vector<double> gain_by_polygon;
    double total = 0.0;
};

ReferenceAddResult reference_add(
    const Scene& scene, const VisibilityCache& cache,
    const std::vector<std::uint32_t>& cover_count,
    std::size_t candidate)
{
    ReferenceAddResult result;
    result.gain_by_polygon.assign(scene.polygons.size(), 0.0);
    for (const std::uint32_t sid : expand(cache, candidate)) {
        if (cover_count[sid] != 0) continue;
        const auto pid = scene.samples[sid].polygon_id;
        result.gain_by_polygon[pid] += scene.samples[sid].weight;
        result.total += scene.samples[sid].weight;
    }
    return result;
}

ReferenceResult reference(
    const Scene& scene, const VisibilityCache& cache,
    const std::vector<std::size_t>& selected,
    const std::vector<std::uint32_t>& cover_count,
    const std::vector<double>& covered,
    std::size_t candidate, double threshold)
{
    const std::size_t l = selected.size();
    const std::size_t pcount = scene.polygons.size();
    std::vector<std::int32_t> owner(scene.samples.size(), -1);
    std::vector<std::vector<double>> loss(l, std::vector<double>(pcount, 0.0));
    std::vector<double> unique_total(l, 0.0);
    for (std::size_t slot = 0; slot < l; ++slot) {
        for (const std::uint32_t sid : expand(cache, selected[slot])) {
            if (cover_count[sid] != 1) continue;
            owner[sid] = static_cast<std::int32_t>(slot);
            loss[slot][scene.samples[sid].polygon_id] += scene.samples[sid].weight;
            unique_total[slot] += scene.samples[sid].weight;
        }
    }
    std::vector<int> base(l, 0);
    for (std::size_t slot = 0; slot < l; ++slot) {
        for (std::size_t pid = 0; pid < pcount; ++pid) {
            if (covered[pid] - loss[slot][pid] + 1e-10 >=
                threshold * scene.polygons[pid].perimeter) ++base[slot];
        }
    }
    std::vector<double> common(pcount, 0.0);
    std::vector<std::vector<double>> restore(l, std::vector<double>(pcount, 0.0));
    std::vector<double> restore_total(l, 0.0);
    double common_total = 0.0;
    for (const std::uint32_t sid : expand(cache, candidate)) {
        const auto pid = scene.samples[sid].polygon_id;
        if (cover_count[sid] == 0) {
            common[pid] += scene.samples[sid].weight;
            common_total += scene.samples[sid].weight;
        } else if (cover_count[sid] == 1) {
            const std::size_t slot = static_cast<std::size_t>(owner[sid]);
            restore[slot][pid] += scene.samples[sid].weight;
            restore_total[slot] += scene.samples[sid].weight;
        }
    }
    int crossings = 0;
    double rescue = 0.0;
    for (std::size_t pid = 0; pid < pcount; ++pid) {
        const double target = threshold * scene.polygons[pid].perimeter;
        if (covered[pid] + 1e-10 >= target) continue;
        rescue += std::min(std::max(0.0, target - covered[pid]), common[pid]);
        if (covered[pid] + common[pid] + 1e-10 >= target) ++crossings;
    }
    const double current_total = sum_covered(covered);
    ReferenceResult best{-1, -1.0, l, crossings, rescue, common_total};
    for (std::size_t slot = 0; slot < l; ++slot) {
        int score = base[slot];
        for (std::size_t pid = 0; pid < pcount; ++pid) {
            const double after = covered[pid] - loss[slot][pid];
            const double gain = common[pid] + restore[slot][pid];
            const double target = threshold * scene.polygons[pid].perimeter;
            if (after + 1e-10 < target && after + gain + 1e-10 >= target) ++score;
        }
        const double total = current_total - unique_total[slot] + common_total + restore_total[slot];
        if (best.slot == l || score > best.score ||
            (score == best.score && total > best.total + 1e-12) ||
            (score == best.score && std::abs(total - best.total) <= 1e-12 && slot < best.slot)) {
            best.score = score; best.total = total; best.slot = slot;
        }
    }
    return best;
}

int main() {
    Scene scene;
    scene.polygons = {{6.0}, {6.0}, {6.0}};
    for (std::uint32_t pid = 0; pid < 3; ++pid) {
        for (int i = 0; i < 6; ++i) scene.samples.push_back({pid, 0, 1.0});
    }
    scene.sample_weight_prefix.assign(scene.samples.size(), 0.0);
    double ring_total = 0.0;
    for (std::size_t i = 0; i < scene.samples.size(); ++i) {
        if (i == 0 || scene.samples[i - 1].polygon_id != scene.samples[i].polygon_id ||
            scene.samples[i - 1].ring_id != scene.samples[i].ring_id) ring_total = 0.0;
        ring_total += scene.samples[i].weight;
        scene.sample_weight_prefix[i] = ring_total;
    }
    VisibilityCache cache;
    cache.rows = {
        {{0,4}, {6,3}, {12,2}},
        {{2,4}, {8,4}, {15,3}},
        {{1,2}, {10,2}, {12,4}},
        {{0,6}, {7,2}, {14,3}},
        {{4,2}, {6,6}, {12,6}},
        {{0,1}, {9,3}, {17,1}},
    };
    scene.candidates.resize(cache.rows.size(), 0);
    const std::vector<std::size_t> selected{0,1,2};
    std::vector<std::uint8_t> selected_flag(cache.rows.size(), 0);
    std::vector<std::uint32_t> cover_count(scene.samples.size(), 0);
    std::vector<double> covered(scene.polygons.size(), 0.0);
    for (const std::size_t candidate : selected) {
        selected_flag[candidate] = 1;
        for (const std::uint32_t sid : expand(cache, candidate)) {
            if (cover_count[sid]++ == 0) covered[scene.samples[sid].polygon_id] += scene.samples[sid].weight;
        }
    }
    const ArcCoverageUnion add_profile =
        build_arc_coverage_union(scene, cache, selected);
    const std::vector<ArcCoverageRun>& add_covered_runs =
        add_profile.covered_runs;
    for (std::size_t candidate = 0; candidate < cache.rows.size(); ++candidate) {
        if (selected_flag[candidate]) continue;
        std::vector<PolygonWeight> actual_gain;
        double actual_total = 0.0;
        std::uint64_t evaluated_arcs = 0;
        std::uint64_t intersected_runs = 0;
        accumulate_candidate_novel_arc_gain(
            scene, cache, candidate, add_covered_runs,
            actual_gain, actual_total, evaluated_arcs, intersected_runs);
        const ReferenceAddResult expected =
            reference_add(scene, cache, cover_count, candidate);
        std::vector<double> dense_actual(scene.polygons.size(), 0.0);
        for (const PolygonWeight& gain : actual_gain) {
            dense_actual[gain.polygon_id] += gain.weight;
        }
        for (std::size_t pid = 0; pid < scene.polygons.size(); ++pid) {
            if (std::abs(dense_actual[pid] - expected.gain_by_polygon[pid]) > 1e-10) {
                std::cerr << "add polygon mismatch for candidate " << candidate
                          << ", polygon " << pid << "\n";
                return 3;
            }
        }
        if (std::abs(actual_total - expected.total) > 1e-10 || evaluated_arcs == 0) {
            std::cerr << "add total mismatch for candidate " << candidate << "\n";
            return 4;
        }
    }

    constexpr double threshold = 0.75;
    const std::vector<std::size_t> insertion_scan{3, 4, 5};
    const ArcInsertionProposal insertion = evaluate_best_arc_insertion(
        scene, cache, ArcCandidateScan::listed(insertion_scan), selected_flag,
        std::numeric_limits<std::size_t>::max(), add_covered_runs,
        covered, threshold, 0.50, sum_covered(covered), 18.0, 1e-12, 1);
    ArcInsertionProposal expected_insertion;
    const int base_score = solution_score(scene, covered, threshold);
    for (const std::size_t candidate : insertion_scan) {
        const ReferenceAddResult gain = reference_add(scene, cache, cover_count, candidate);
        ArcInsertionProposal proposal;
        proposal.candidate = candidate;
        proposal.score = base_score;
        for (std::size_t pid = 0; pid < scene.polygons.size(); ++pid) {
            const double target = threshold * scene.polygons[pid].perimeter;
            if (covered[pid] + 1e-10 < target &&
                covered[pid] + gain.gain_by_polygon[pid] + 1e-10 >= target) {
                ++proposal.score;
            }
        }
        proposal.total = sum_covered(covered) + gain.total;
        std::vector<double> proposal_covered = covered;
        for (std::size_t pid = 0; pid < scene.polygons.size(); ++pid) {
            proposal_covered[pid] += gain.gain_by_polygon[pid];
        }
        proposal.quality = static_cast<double>(proposal.score) +
            0.70 * near_threshold_progress(
                scene, proposal_covered, threshold, 0.50) +
            0.299 * (proposal.total / 18.0);
        if (better_arc_insertion_proposal(proposal, expected_insertion, 1e-12)) {
            expected_insertion = proposal;
        }
    }
    if (insertion.candidate != expected_insertion.candidate ||
        insertion.score != expected_insertion.score ||
        std::abs(insertion.total - expected_insertion.total) > 1e-10 ||
        insertion.evaluated != insertion_scan.size()) {
        std::cerr << "arc insertion helper mismatch\n";
        return 5;
    }

    const auto actual = evaluate_replacement_neighborhood(
        scene, cache, selected, selected_flag, covered, threshold);
    const ArcReplacementEvaluationContext delta_context =
        build_arc_replacement_evaluation_context(
            scene, cache, selected, covered, threshold);
    const SearchQuality base_quality = evaluate_search_quality(
        scene, covered, threshold, 0.50);
    for (std::size_t candidate = 0; candidate < cache.rows.size(); ++candidate) {
        if (selected_flag[candidate]) continue;
        const ReferenceResult expected = reference(
            scene, cache, selected, cover_count, covered, candidate, threshold);
        if (actual.best_score[candidate] != expected.score ||
            actual.best_slot[candidate] != expected.slot ||
            actual.addition_crossings[candidate] != expected.crossings ||
            std::abs(actual.best_total[candidate] - expected.total) > 1e-10 ||
            std::abs(actual.rescue_gain[candidate] - expected.rescue) > 1e-10 ||
            std::abs(actual.novel_gain[candidate] - expected.novel) > 1e-10) {
            std::cerr << "mismatch for candidate " << candidate << "\n";
            return 2;
        }
        const ArcReplacementCoverageDelta delta =
            build_arc_replacement_coverage_delta(
                scene, cache, candidate, expected.slot, delta_context);
        std::vector<double> incremental = covered;
        apply_arc_replacement_coverage(scene, delta, incremental);
        std::vector<std::size_t> trial = selected;
        trial[expected.slot] = candidate;
        const std::vector<double> exact =
            build_arc_coverage_union(scene, cache, trial).covered_by_polygon;
        if (incremental.size() != exact.size()) return 7;
        for (std::size_t polygon = 0; polygon < exact.size(); ++polygon) {
            if (std::abs(incremental[polygon] - exact[polygon]) > 1e-10) {
                std::cerr << "incremental replacement mismatch for candidate "
                          << candidate << ", polygon " << polygon << "\n";
                return 7;
            }
        }
        const SearchQuality incremental_quality =
            evaluate_arc_replacement_quality(
                scene, covered, threshold, 0.50, base_quality, delta);
        const SearchQuality exact_quality = evaluate_search_quality(
            scene, exact, threshold, 0.50);
        if (incremental_quality.score != exact_quality.score ||
            std::abs(incremental_quality.near_progress -
                     exact_quality.near_progress) > 1e-10 ||
            std::abs(incremental_quality.total - exact_quality.total) > 1e-10) {
            std::cerr << "incremental replacement quality mismatch\n";
            return 8;
        }
    }
    const std::vector<std::size_t> sampled_removal_one{1};
    const auto sampled_one = evaluate_replacement_neighborhood(
        scene, cache, selected, selected_flag, covered, threshold,
        nullptr, nullptr, nullptr,
        std::numeric_limits<std::size_t>::max(), &insertion_scan, true,
        &delta_context, nullptr, 0, &sampled_removal_one);
    if (sampled_one.evaluated_candidates != insertion_scan.size() ||
        sampled_one.evaluated_removal_slots != insertion_scan.size()) {
        std::cerr << "sampled-removal single-slot accounting mismatch\n";
        return 9;
    }
    for (std::size_t i = 0; i < insertion_scan.size(); ++i) {
        if (sampled_one.best_slot[i] != 1) {
            std::cerr << "sampled-removal evaluator escaped requested slot\n";
            return 9;
        }
    }
    const std::vector<std::size_t> sampled_removal_two{0, 2};
    const auto sampled_two = evaluate_replacement_neighborhood(
        scene, cache, selected, selected_flag, covered, threshold,
        nullptr, nullptr, nullptr,
        std::numeric_limits<std::size_t>::max(), &insertion_scan, true,
        &delta_context, nullptr, 0, &sampled_removal_two);
    if (sampled_two.evaluated_removal_slots !=
        insertion_scan.size() * sampled_removal_two.size()) {
        std::cerr << "sampled-removal two-slot accounting mismatch\n";
        return 10;
    }
    for (std::size_t i = 0; i < insertion_scan.size(); ++i) {
        if (sampled_two.best_slot[i] != 0 && sampled_two.best_slot[i] != 2) {
            std::cerr << "sampled-removal evaluator selected outside subset\n";
            return 10;
        }
    }

    Scene sparse_scene;
    sparse_scene.polygons = {{2.0}, {2.0}, {2.0}, {2.0}, {2.0}};
    for (std::uint32_t pid = 0; pid < 5; ++pid) {
        sparse_scene.samples.push_back({pid, 0, 1.0});
        sparse_scene.samples.push_back({pid, 0, 1.0});
    }
    sparse_scene.sample_weight_prefix.assign(sparse_scene.samples.size(), 0.0);
    for (std::size_t i = 0; i < sparse_scene.samples.size(); ++i) {
        const bool new_ring = i == 0 ||
            sparse_scene.samples[i - 1].polygon_id !=
                sparse_scene.samples[i].polygon_id;
        sparse_scene.sample_weight_prefix[i] =
            (new_ring ? 0.0 : sparse_scene.sample_weight_prefix[i - 1]) + 1.0;
    }
    VisibilityCache sparse_cache;
    sparse_cache.rows = {
        {{0, 2}}, {{2, 2}}, {{4, 2}}, {{6, 2}}, {{8, 2}},
    };
    sparse_scene.candidates.resize(sparse_cache.rows.size(), 0);
    const std::vector<std::size_t> sparse_selected{0, 1, 2, 3};
    std::vector<std::uint8_t> sparse_selected_flag(5, 0);
    for (const std::size_t candidate : sparse_selected) {
        sparse_selected_flag[candidate] = 1;
    }
    const auto sparse_union =
        build_arc_coverage_union(sparse_scene, sparse_cache, sparse_selected);
    const std::vector<std::size_t> sparse_subset{4};
    const auto sparse_eval = evaluate_replacement_neighborhood(
        sparse_scene, sparse_cache, sparse_selected, sparse_selected_flag,
        sparse_union.covered_by_polygon, 0.5,
        nullptr, nullptr, nullptr,
        std::numeric_limits<std::size_t>::max(), &sparse_subset, true);
    if (sparse_eval.evaluated_candidates != 1 ||
        sparse_eval.evaluated_removal_slots != 1) {
        std::cerr << "sparse removal baseline did not reduce to one slot\\n";
        return 3;
    }

    // Scaling guard: 20,000 insertion candidates and 128 selected guards
    // should evaluate one exact removal slot per candidate, not 2.56 million
    // candidate/slot pairs.
    Scene scaling_scene;
    scaling_scene.polygons = {{64.0}};
    for (std::uint32_t sid = 0; sid < 64; ++sid) {
        scaling_scene.samples.push_back({0, 0, 1.0});
        scaling_scene.sample_weight_prefix.push_back(
            static_cast<double>(sid + 1U));
    }
    VisibilityCache scaling_cache;
    constexpr std::size_t scaling_selected_count = 128;
    constexpr std::size_t scaling_candidate_count = 20000;
    scaling_cache.rows.reserve(
        scaling_selected_count + scaling_candidate_count);
    for (std::size_t i = 0; i < scaling_selected_count; ++i) {
        scaling_cache.rows.push_back({{0, 64}});
    }
    for (std::size_t i = 0; i < scaling_candidate_count; ++i) {
        scaling_cache.rows.push_back({{0, 1}});
    }
    scaling_scene.candidates.resize(scaling_cache.rows.size(), 0);
    std::vector<std::size_t> scaling_selected(scaling_selected_count);
    std::iota(scaling_selected.begin(), scaling_selected.end(), 0U);
    std::vector<std::uint8_t> scaling_selected_flag(
        scaling_cache.rows.size(), 0);
    for (const std::size_t candidate : scaling_selected) {
        scaling_selected_flag[candidate] = 1;
    }
    const auto scaling_union = build_arc_coverage_union(
        scaling_scene, scaling_cache, scaling_selected);
    std::vector<std::size_t> scaling_subset(scaling_candidate_count);
    std::iota(
        scaling_subset.begin(), scaling_subset.end(),
        scaling_selected_count);
    const auto scaling_eval = evaluate_replacement_neighborhood(
        scaling_scene, scaling_cache, scaling_selected,
        scaling_selected_flag, scaling_union.covered_by_polygon, 0.5,
        nullptr, nullptr, nullptr,
        std::numeric_limits<std::size_t>::max(), &scaling_subset, true);
    if (scaling_eval.evaluated_candidates != scaling_candidate_count ||
        scaling_eval.evaluated_removal_slots != scaling_candidate_count) {
        std::cerr << "sparse scaling path regressed to selected-cardinality scan\n";
        return 5;
    }

    const auto expired_deadline =
        std::chrono::steady_clock::now() - std::chrono::milliseconds(1);
    const auto interrupted = evaluate_replacement_neighborhood(
        scene, cache, selected, selected_flag, covered, threshold,
        nullptr, nullptr, nullptr,
        std::numeric_limits<std::size_t>::max(), nullptr, false,
        nullptr, &expired_deadline);
    if (!interrupted.deadline_exhausted ||
        interrupted.evaluated_candidates != 0) {
        std::cerr << "expired 1-swap deadline was not respected\n";
        return 6;
    }
    std::cout << "RINGARC23 exact C++ add/sparse-1-swap/scaling/deadline harness passed\n";
    return 0;
}
'''

def compiler_candidates() -> list[list[str]]:
    candidates: list[list[str]] = []

    def add(command: str | None) -> None:
        if not command:
            return
        parts = shlex.split(command)
        if parts and parts not in candidates:
            candidates.append(parts)

    # Prefer the same compiler as the main CMake build.  The Slurm script
    # exports CXX, and an already-configured tree records it in CMakeCache.txt.
    add(os.environ.get("CXX"))
    cache = ROOT / "build" / "CMakeCache.txt"
    if cache.is_file():
        for line in cache.read_text(errors="replace").splitlines():
            if line.startswith("CMAKE_CXX_COMPILER:FILEPATH=") or line.startswith("CMAKE_CXX_COMPILER:STRING="):
                add(line.split("=", 1)[1].strip())
                break

    add(shutil.which("c++"))
    add(shutil.which("g++"))
    return candidates


def choose_compiler_and_standard() -> tuple[list[str], str]:
    probe = "#include <cstdint>\nint main() { std::uint32_t x = 0; return static_cast<int>(x); }\n"
    diagnostics: list[str] = []
    for compiler in compiler_candidates():
        for standard in ("-std=c++20", "-std=c++2a"):
            result = subprocess.run(
                compiler + [standard, "-x", "c++", "-fsyntax-only", "-"],
                input=probe, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if result.returncode == 0:
                return compiler, standard
            diagnostics.append(
                f"{' '.join(compiler)} {standard}: "
                f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'failed'}"
            )
    detail = "\n  ".join(diagnostics) if diagnostics else "no C++ compiler found on PATH"
    raise RuntimeError(
        "No usable C++20 compiler was found. Tried both -std=c++20 and "
        f"the GCC compatibility spelling -std=c++2a:\n  {detail}"
    )


with tempfile.TemporaryDirectory(prefix="ringarc9-harness-") as tmp:
    cpp = Path(tmp) / "harness.cpp"
    exe = Path(tmp) / "harness"
    cpp.write_text(PRELUDE + "\n" + BLOCK + "\n" + POSTLUDE)
    compiler, standard = choose_compiler_and_standard()
    print(f"RINGARC11 harness compiler: {' '.join(compiler)} {standard}")
    subprocess.run(compiler + [
        standard, "-O2", "-Wall", "-Wextra", "-Wpedantic",
        "-Wconversion", "-Wshadow", str(cpp), "-o", str(exe)
    ], check=True)
    subprocess.run([str(exe)], check=True)
