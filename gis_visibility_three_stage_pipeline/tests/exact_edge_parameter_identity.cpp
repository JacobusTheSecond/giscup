#include <CGAL/Exact_predicates_exact_constructions_kernel.h>
#include <CGAL/number_utils.h>

#include <array>
#include <cassert>
#include <iostream>
#include <map>

using Kernel = CGAL::Exact_predicates_exact_constructions_kernel;
using FT = Kernel::FT;
using Point = Kernel::Point_2;

FT edge_parameter(
    const Point& a,
    const Point& b,
    const Point& query,
    const Point& ray_witness)
{
    const FT sx = b.x() - a.x();
    const FT sy = b.y() - a.y();
    const FT rx = ray_witness.x() - query.x();
    const FT ry = ray_witness.y() - query.y();
    const FT denominator = sx * ry - sy * rx;
    assert(denominator != FT(0));
    const FT qax = query.x() - a.x();
    const FT qay = query.y() - a.y();
    return (qax * ry - qay * rx) / denominator;
}

int main() {
    const Point edge_a(10, 0);
    const Point edge_b(10, 10);

    // Two independent visibility rays meet the same mathematical edge point
    // (10, 5), despite having different query points and witnesses.
    const FT first = edge_parameter(edge_a, edge_b, Point(0, 0), Point(5, 2.5));
    const FT second = edge_parameter(edge_a, edge_b, Point(0, 10), Point(5, 7.5));
    assert(first == FT(1) / FT(2));
    assert(first == second);

    // A genuinely different point on the same edge must not be merged.
    const FT distinct = edge_parameter(edge_a, edge_b, Point(0, 0), Point(5, 3));
    assert(distinct != first);

    // Production identity includes wiggle state. Equal edge parameters merge
    // within a state, while the boundary and normal-offset candidates remain
    // intentionally distinct.
    std::array<std::map<FT, int>, 2> identity;
    assert(identity[0].emplace(first, 1).second);
    assert(!identity[0].emplace(second, 2).second);
    assert(identity[0].emplace(distinct, 3).second);
    assert(identity[1].emplace(first, 4).second);

    std::cout << "same_parameter=" << CGAL::to_double(first) << '\n';
    std::cout << "boundary_unique=" << identity[0].size() << '\n';
    std::cout << "wiggled_unique=" << identity[1].size() << '\n';
}
