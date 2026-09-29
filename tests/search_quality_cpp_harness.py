#!/usr/bin/env python3
from pathlib import Path
import subprocess, tempfile
root=Path(__file__).resolve().parents[1]
source=(root/'src/main.cpp').read_text()
start=source.index('[[nodiscard]] double near_threshold_contribution(')
end=source.index('\n\nclass OptimizationTimeline', start)
fragment=source[start:end]
code=r'''
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <numeric>
#include <vector>
struct PolygonData { std::uint32_t internal_id=0; double perimeter=0; std::vector<int> rings; };
struct Point { double px=0,py=0; };
struct SegmentData { Point a,b; std::uint32_t polygon_id=0, ring_id=0; };
struct Scene { std::vector<PolygonData> polygons; std::vector<SegmentData> segments; };
double x(const Point&p){return p.px;} double y(const Point&p){return p.py;}
bool coverage_qualified(double v,double t){return v+1e-10>=t;}
int solution_score(const Scene&s,const std::vector<double>&c,double t){int z=0;for(size_t i=0;i<s.polygons.size();++i)if(coverage_qualified(c[i],t*s.polygons[i].perimeter))++z;return z;}
double sum_covered(const std::vector<double>& c){return std::accumulate(c.begin(),c.end(),0.0);}
'''+fragment+r'''
int main(){
 Scene s; s.polygons={{0,100,{}},{1,100,{}},{2,100,{}}};
 std::vector<double>a={75,40,100}, b={80,40,100}, c={74,100,100};
 auto qa=evaluate_search_quality(s,a,.8,.5), qb=evaluate_search_quality(s,b,.8,.5), qc=evaluate_search_quality(s,c,.8,.5);
 if(!better_search_quality(qb,qa,1e-9)) return 1; // qualifies one more
 if(better_search_quality(qa,qb,1e-9)) return 2;
 if(!better_search_quality(qc,qa,1e-9)) return 3; // same score, stronger near progress
 if(!(scalar_search_quality(qb,300)>scalar_search_quality(qa,300))) return 4;
 s.segments.push_back({{0,0},{1,0},0,0}); auto f1=scene_geometry_fingerprint(s); s.segments[0].b.px=2; auto f2=scene_geometry_fingerprint(s); if(f1==f2) return 5;
 std::cout<<"RINGARC25.1 search-quality harness passed\n";
}
'''
with tempfile.TemporaryDirectory() as td:
 cpp=Path(td)/'h.cpp'; exe=Path(td)/'h'; cpp.write_text(code)
 subprocess.run(['g++','-std=c++20','-O2',str(cpp),'-o',str(exe)],check=True)
 subprocess.run([str(exe)],check=True)
