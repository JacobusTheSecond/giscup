#!/usr/bin/env python3
"""Compile the production stepped-threshold helper and add-cache class with a synthetic exact union model."""
from __future__ import annotations
import os, shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'src' / 'main.cpp').read_text()
THRESHOLD = SOURCE[SOURCE.index('struct EffectiveThresholdState {'):SOURCE.index('void write_step_snapshot(', SOURCE.index('struct EffectiveThresholdState {'))]
CACHE = SOURCE[SOURCE.index('class SteppedAddCache {'):SOURCE.index('void write_submission_block(', SOURCE.index('class SteppedAddCache {'))]

PRELUDE = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <span>
#include <stdexcept>
#include <vector>

struct PolygonData { double perimeter = 1.0; };
struct Scene { std::vector<PolygonData> polygons; std::vector<std::uint32_t> candidates; };
struct ArcCoverageRun {};
struct PolygonWeight { std::uint32_t polygon_id = 0; double weight = 0.0; };

class VisibilityCache {
public:
    std::vector<std::vector<double>> weight;
    std::vector<std::uint8_t> search_mask;
    bool is_search_candidate(std::size_t c) const {
        return c < weight.size() && (search_mask.empty() || search_mask[c] != 0);
    }
    std::size_t search_candidate_count() const {
        if (search_mask.empty()) return weight.size();
        return static_cast<std::size_t>(std::count(search_mask.begin(), search_mask.end(), std::uint8_t{1}));
    }
    template <typename Fn>
    void for_each_arc(std::size_t c, Fn&& fn) const {
        for (std::size_t p=0; p<weight.at(c).size(); ++p) {
            if (weight[c][p] > 0) fn(static_cast<std::uint32_t>(p), 1U);
        }
    }
};
std::uint32_t scene_sample_polygon_id(const Scene&, std::uint32_t begin) { return begin; }

struct CandidatePolygonMap {
    std::vector<std::uint64_t> offsets;
    std::vector<std::uint32_t> polygon_ids;
    std::size_t size() const { return offsets.empty() ? 0 : offsets.size()-1; }
    std::span<const std::uint32_t> polygons(std::size_t c) const {
        auto b=offsets.at(c), e=offsets.at(c+1);
        return {polygon_ids.data()+static_cast<std::size_t>(b), static_cast<std::size_t>(e-b)};
    }
};
struct CandidatePolygonReverseIndex {
    std::vector<std::uint64_t> offsets;
    std::vector<std::uint32_t> candidate_ids;
    bool empty() const { return offsets.empty(); }
    std::span<const std::uint32_t> candidates(std::size_t p) const {
        auto b=offsets.at(p), e=offsets.at(p+1);
        return {candidate_ids.data()+static_cast<std::size_t>(b), static_cast<std::size_t>(e-b)};
    }
};

int solution_score(const Scene& scene, const std::vector<double>& covered, double threshold) {
    int score=0;
    for (std::size_t p=0;p<scene.polygons.size();++p)
        if (covered[p]+1e-10 >= threshold*scene.polygons[p].perimeter) ++score;
    return score;
}
double sum_covered(const std::vector<double>& v) { return std::accumulate(v.begin(),v.end(),0.0); }

struct ArcInsertionProposal {
    std::size_t candidate = std::numeric_limits<std::size_t>::max();
    int score = std::numeric_limits<int>::min();
    double total = 0.0;
    double quality = -std::numeric_limits<double>::infinity();
    std::size_t evaluated = 0;
    std::uint64_t evaluated_arcs = 0;
    std::uint64_t intersected_runs = 0;
    std::size_t evaluated_blocks = 0, pruned_blocks = 0, pruned_candidates = 0;
};
bool better_arc_insertion_proposal(const ArcInsertionProposal& a, const ArcInsertionProposal& b, double eps) {
    return a.quality > b.quality + 1e-12 ||
      (std::abs(a.quality-b.quality)<=1e-12 &&
       (a.score>b.score || (a.score==b.score && a.total>b.total+eps) ||
        (a.score==b.score && std::abs(a.total-b.total)<=eps && a.candidate<b.candidate)));
}
struct ArcInsertionGain { int score_gain=0; double covered_gain=0; std::uint64_t evaluated_arcs=0, intersected_runs=0; };
ArcInsertionGain evaluate_arc_insertion_gain(
    const Scene& scene, const VisibilityCache& cache, std::size_t c,
    const std::vector<ArcCoverageRun>&, const std::vector<double>& covered,
    double threshold, double near_floor, std::vector<PolygonWeight>& scratch)
{
    (void)near_floor; scratch.clear(); ArcInsertionGain g;
    for (std::size_t p=0;p<scene.polygons.size();++p) {
        const double w=cache.weight[c][p]; if (!(w>0)) continue;
        ++g.evaluated_arcs;
        const double novel=std::max(0.0,w-covered[p]);
        if (!(novel>0)) continue;
        scratch.push_back({static_cast<std::uint32_t>(p),novel});
        g.covered_gain += novel;
        const double target=threshold*scene.polygons[p].perimeter;
        if (covered[p]+1e-10<target && covered[p]+novel+1e-10>=target) ++g.score_gain;
    }
    return g;
}
'''

POST = r'''
CandidatePolygonMap forward_map(const VisibilityCache& cache, std::size_t polygons) {
    CandidatePolygonMap m; m.offsets.assign(cache.weight.size()+1,0);
    for (std::size_t c=0;c<cache.weight.size();++c) {
        if (cache.is_search_candidate(c)) {
            for (std::size_t p=0;p<polygons;++p) if (cache.weight[c][p]>0) m.polygon_ids.push_back(static_cast<std::uint32_t>(p));
        }
        m.offsets[c+1]=m.polygon_ids.size();
    }
    return m;
}
CandidatePolygonReverseIndex reverse_map(const CandidatePolygonMap& f, std::size_t polygons) {
    CandidatePolygonReverseIndex r; r.offsets.assign(polygons+1,0);
    for (std::size_t c=0;c<f.size();++c) for (auto p:f.polygons(c)) ++r.offsets[p+1];
    for (std::size_t p=0;p<polygons;++p) r.offsets[p+1]+=r.offsets[p];
    r.candidate_ids.resize(r.offsets.back()); auto cur=r.offsets;
    for (std::size_t c=0;c<f.size();++c) for (auto p:f.polygons(c)) r.candidate_ids[cur[p]++]=static_cast<std::uint32_t>(c);
    return r;
}
std::vector<double> rebuild(const VisibilityCache& cache, const std::vector<std::uint8_t>& selected, std::size_t polygons) {
    std::vector<double> covered(polygons,0.0);
    for (std::size_t c=0;c<selected.size();++c) if (selected[c])
        for (std::size_t p=0;p<polygons;++p) covered[p]=std::max(covered[p],cache.weight[c][p]);
    return covered;
}
ArcInsertionProposal brute(const Scene& scene,const VisibilityCache& cache,const std::vector<std::uint8_t>& selected,const std::vector<double>& covered,double t) {
    ArcInsertionProposal best; std::vector<PolygonWeight> scratch; std::vector<ArcCoverageRun> runs;
    const int base=solution_score(scene,covered,t); const double total=sum_covered(covered);
    for(std::size_t c=0;c<cache.weight.size();++c) if(!selected[c]) {
        auto g=evaluate_arc_insertion_gain(scene,cache,c,runs,covered,t,0.50,scratch);
        ArcInsertionProposal p; p.candidate=c; p.score=base+g.score_gain; p.total=total+g.covered_gain; p.quality=p.score+0.999*p.total/std::max(1.0,double(scene.polygons.size()));
        if(better_arc_insertion_proposal(p,best,1e-12)) best=p;
    }
    return best;
}
int main(){
    auto a=effective_threshold_state(1,10000,0.75,1.7,100);
    auto b=effective_threshold_state(100,10000,0.75,1.7,100);
    auto c=effective_threshold_state(101,10000,0.75,1.7,100);
    auto d=effective_threshold_state(200,10000,0.75,1.7,100);
    if(a.step_index!=1 || b.step_index!=1 || c.step_index!=2 || d.step_index!=2) return 1;
    if(std::abs(a.threshold-0.75*std::pow(0.01,1.7))>1e-15 || a.threshold!=b.threshold) return 2;
    if(std::abs(c.threshold-0.75*std::pow(0.02,1.7))>1e-15 || c.threshold!=d.threshold) return 3;
    if(std::abs(effective_threshold_state(10000,10000,0.75,1.7,100).threshold-0.75)>1e-15) return 4;

    constexpr std::size_t P=64,C=40000; Scene scene; scene.polygons.resize(P); scene.candidates.resize(C);
    VisibilityCache cache; cache.weight.assign(C,std::vector<double>(P,0.0));
    std::mt19937_64 rng(123); std::uniform_int_distribution<int> pd(0,P-1); std::uniform_real_distribution<double> wd(0.05,1.0);
    for(std::size_t x=0;x<C;++x) for(int j=0;j<4;++j) cache.weight[x][pd(rng)]=wd(rng);
    auto f=forward_map(cache,P); auto r=reverse_map(f,P); std::vector<std::uint8_t> selected(C,0); auto covered=rebuild(cache,selected,P); std::vector<ArcCoverageRun> runs;
    SteppedAddCache sc; double t=0.2; sc.rebuild(scene,cache,selected,runs,covered,t,1,1,false);
    for(int round=0;round<30;++round){
        auto x=sc.best(scene,selected,covered,t,double(P),1e-12); auto y=brute(scene,cache,selected,covered,t); if(x.candidate!=y.candidate) return 10;
        selected[x.candidate]=1; covered=rebuild(cache,selected,P); std::vector<std::size_t> changed{x.candidate}; sc.refresh_changed(scene,cache,f,r,changed,selected,runs,covered,t,1,false);
    }
    // Exact swap invalidation: changed old/new guards are sufficient.
    for(int round=0;round<20;++round){
        std::size_t out=0; while(out<C && !selected[out]) ++out;
        std::size_t in=C-1; while(in>0 && selected[in]) --in;
        selected[out]=0; selected[in]=1; covered=rebuild(cache,selected,P); std::vector<std::size_t> changed{out,in}; sc.refresh_changed(scene,cache,f,r,changed,selected,runs,covered,t,1,false);
        auto x=sc.best(scene,selected,covered,t,double(P),1e-12); auto y=brute(scene,cache,selected,covered,t); if(x.candidate!=y.candidate) return 20;
    }
    // Resume/import edge case: a selected guard may be a non-search duplicate.
    // Production forward CSR omits it, so refresh_changed must recover its
    // polygons from the exact cache row when that selected guard changes.
    std::size_t duplicate = C-2;
    std::size_t representative = C-3;
    cache.weight[duplicate] = cache.weight[representative];
    cache.search_mask.assign(C, 1);
    cache.search_mask[duplicate] = 0;
    f=forward_map(cache,P); r=reverse_map(f,P);
    selected.assign(C,0); selected[duplicate]=1;
    covered=rebuild(cache,selected,P);
    sc=SteppedAddCache{}; sc.rebuild(scene,cache,selected,runs,covered,t,1,1,false);
    std::size_t incoming=0; while(incoming<C && (incoming==duplicate || selected[incoming])) ++incoming;
    selected[duplicate]=0; selected[incoming]=1; covered=rebuild(cache,selected,P);
    std::vector<std::size_t> resume_changed{duplicate,incoming};
    sc.refresh_changed(scene,cache,f,r,resume_changed,selected,runs,covered,t,1,false);
    auto rx=sc.best(scene,selected,covered,t,double(P),1e-12);
    auto ry=brute(scene,cache,selected,covered,t);
    if(rx.candidate!=ry.candidate) return 25;

    t=0.3; sc.rebuild(scene,cache,selected,runs,covered,t,2,1,false);
    if(sc.best(scene,selected,covered,t,double(P),1e-12).candidate!=brute(scene,cache,selected,covered,t).candidate) return 30;
    std::cout<<"RINGARC15 stepped threshold and exact incremental add-cache harness passed\n";
}
'''

program = PRELUDE + THRESHOLD + CACHE + POST
compiler = os.environ.get('CXX') or shutil.which('c++') or shutil.which('g++')
if not compiler: raise SystemExit('no C++ compiler found')
with tempfile.TemporaryDirectory(prefix='ringarc15-stepcache-') as td:
    cpp=Path(td)/'harness.cpp'; exe=Path(td)/'harness'; cpp.write_text(program)
    subprocess.run([compiler,'-std=c++20','-O2','-Wall','-Wextra','-Wpedantic',str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
