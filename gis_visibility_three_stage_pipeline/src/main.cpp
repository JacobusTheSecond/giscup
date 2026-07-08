#include <gdal_priv.h>
#include <ogrsf_frmts.h>
#include <cpl_conv.h>
#include <cpl_string.h>

#include <boost/geometry.hpp>
#include <boost/geometry/index/rtree.hpp>
#include <boost/variant/get.hpp>

#include <CGAL/Exact_predicates_inexact_constructions_kernel.h>
#include <CGAL/Exact_predicates_exact_constructions_kernel.h>
#include <CGAL/Arr_segment_traits_2.h>
#include <CGAL/Arrangement_2.h>
#include <CGAL/Arr_naive_point_location.h>
#include <CGAL/Triangular_expansion_visibility_2.h>
#include <CGAL/intersections.h>
#include <CGAL/Polygon_2_algorithms.h>
#include <CGAL/Constrained_Delaunay_triangulation_2.h>
#include <CGAL/Constrained_triangulation_face_base_2.h>
#include <CGAL/Triangulation_face_base_with_info_2.h>
#include <CGAL/Triangulation_vertex_base_2.h>
#include <CGAL/Triangulation_data_structure_2.h>
#include <CGAL/number_utils.h>
#include <CGAL/version.h>

#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model_solver.h"

#include <algorithm>
#include <array>
#include <bit>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <exception>
#include <filesystem>
#include <fstream>
#include <future>
#include <iomanip>
#include <iostream>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <numeric>
#include <queue>
#include <random>
#include <optional>
#include <sstream>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <variant>
#include <utility>
#include <vector>

#ifdef GIS_CUP_HAS_OPENMP
#include <omp.h>
#endif

namespace bg = boost::geometry;
namespace bgi = boost::geometry::index;
namespace fs = std::filesystem;

constexpr std::string_view PROGRAM_REVISION = "PIPELINE1";

using Point = bg::model::d2::point_xy<double>;
using Box = bg::model::box<Point>;
using Segment = bg::model::segment<Point>;
using SegmentValue = std::pair<Box, std::uint32_t>;
using PointValue = std::pair<Point, std::uint32_t>;
using SegmentRTree = bgi::rtree<SegmentValue, bgi::rstar<16>>;
using PointRTree = bgi::rtree<PointValue, bgi::rstar<16>>;
using PolygonValue = std::pair<Box, std::uint32_t>;
using PolygonRTree = bgi::rtree<PolygonValue, bgi::rstar<16>>;

using CgalKernel = CGAL::Exact_predicates_inexact_constructions_kernel;
using CgalPoint = CgalKernel::Point_2;

using VerifierKernel = CGAL::Exact_predicates_exact_constructions_kernel;
using VerifierFT = VerifierKernel::FT;
using VerifierPoint = VerifierKernel::Point_2;
using VerifierSegment = VerifierKernel::Segment_2;
using VerifierTraits = CGAL::Arr_segment_traits_2<VerifierKernel>;
using VerifierArrangement = CGAL::Arrangement_2<VerifierTraits>;
using VerifierVisibility =
    CGAL::Triangular_expansion_visibility_2<VerifierArrangement, CGAL::Tag_false>;
using VerifierPointLocation = CGAL::Arr_naive_point_location<VerifierArrangement>;

template <typename T, typename Variant>
[[nodiscard]] const T* cgal_variant_get_if(const Variant& value) {
    if constexpr (requires(const Variant* variant) { std::get_if<T>(variant); }) {
        return std::get_if<T>(&value);
    } else {
        return boost::get<T>(&value);
    }
}

struct CdtFaceInfo {
    int nesting_level = -1;
    std::uint32_t id = std::numeric_limits<std::uint32_t>::max();
    [[nodiscard]] bool in_domain() const { return nesting_level >= 0 && (nesting_level % 2) == 1; }
};

using CdtVb = CGAL::Triangulation_vertex_base_2<CgalKernel>;
using CdtFbBase = CGAL::Constrained_triangulation_face_base_2<CgalKernel>;
using CdtFb = CGAL::Triangulation_face_base_with_info_2<CdtFaceInfo, CgalKernel, CdtFbBase>;
using CdtTds = CGAL::Triangulation_data_structure_2<CdtVb, CdtFb>;
using Cdt = CGAL::Constrained_Delaunay_triangulation_2<CgalKernel, CdtTds, CGAL::Exact_predicates_tag>;
using CdtFaceHandle = Cdt::Face_handle;
using CdtVertexHandle = Cdt::Vertex_handle;
using CdtEdge = Cdt::Edge;

constexpr double PI = 3.141592653589793238462643383279502884;
constexpr double TWO_PI = 2.0 * PI;

struct OGRGeometryDeleter {
    void operator()(OGRGeometry* geometry) const noexcept {
        OGRGeometryFactory::destroyGeometry(geometry);
    }
};
using OGRGeometryPtr = std::unique_ptr<OGRGeometry, OGRGeometryDeleter>;

struct GDALDatasetDeleter {
    void operator()(GDALDataset* dataset) const noexcept {
        if (dataset != nullptr) {
            GDALClose(dataset);
        }
    }
};
using GDALDatasetPtr = std::unique_ptr<GDALDataset, GDALDatasetDeleter>;

struct OGRFeatureDeleter {
    void operator()(OGRFeature* feature) const noexcept {
        OGRFeature::DestroyFeature(feature);
    }
};
using OGRFeaturePtr = std::unique_ptr<OGRFeature, OGRFeatureDeleter>;

struct OGRSpatialReferenceDeleter {
    void operator()(OGRSpatialReference* srs) const noexcept {
        if (srs != nullptr) {
            srs->Release();
        }
    }
};
using OGRSpatialReferencePtr = std::unique_ptr<OGRSpatialReference, OGRSpatialReferenceDeleter>;

struct Options {
    fs::path input;
    fs::path output_dir = "gis_cup_output";
    fs::path cache_path;
    fs::path timeline_path; // Defaults to output-dir/optimization_timeline.csv.
    std::size_t k = 10;
    double threshold = 0.8;
    double curve_exponent = 1.0;
    bool search_curve_exponent = false;
    double exponent_min = 0.5;
    double exponent_max = 2.0;
    std::size_t exponent_search_iterations = 3;
    std::size_t exponent_workers = 2;
    // Split each boundary interval between consecutive visibility-polygon
    // candidate locations into this many equal candidate intervals.
    std::size_t candidate_subdivisions = 10;
    // Only visibility-polygon vertices within this distance of the generating
    // original polygon vertex become candidates. Zero means unlimited.
    double vertex_visibility_distance = 0.0;
    // Deprecated compatibility inputs. They are parsed but ignored by the new
    // visibility-induced discretization.
    double boundary_spacing = 1.0;
    double candidate_spacing = 1.0;
    double bbox_padding = 20.0;
    double boundary_epsilon = 1e-6;
    std::size_t max_swap_passes = 0; // 0 means until local optimum.
    double swap_improvement_epsilon = 1e-8;
    bool multi_swap_polish = true;
    // Restricted CP-SAT may replace at most this many incumbent guards.
    std::size_t multi_swap_max_new = 3;
    // Run the restricted neighborhood only after every Nth add/cardinality round.
    std::size_t multi_swap_every = 1;
    // Maximum restricted CP-SAT attempts at one scheduled cardinality.
    // Zero means continue until the first non-improving attempt.
    std::size_t multi_swap_max_rounds = 0;
    std::size_t multi_swap_extra_count = 100;
    double multi_swap_time_limit = 30.0;
    std::size_t multi_swap_partners_per_anchor = 1;
    std::size_t multi_swap_anchor_polygon_samples = 8;
    std::size_t multi_swap_anchor_candidate_samples = 256;
    std::uint64_t multi_swap_anchor_seed = 1;
    bool write_samples = false;
    bool force_rebuild_cache = false;
    bool expand_cache_in_memory = true;
    bool deduplicate_candidates = true;
    std::size_t progress_every = 100;
    std::size_t snapshot_every = 100; // 0 means final state only.
    bool pool_polish = true;
    double polish_extra_fraction = 0.05;
    std::size_t polish_extra_count = 0; // Nonzero overrides the fraction.
    std::size_t polish_max_rounds = 0; // 0 means until no strict improvement.
    double polish_time_limit = 0.0; // 0 means no solver time limit.
    double polish_mip_gap = 0.0; // Kept as a CLI-compatible CP-SAT relative gap.
    std::size_t polish_workers = 0; // 0 inherits --threads; if both are 0, CP-SAT uses all cores.
    bool polish_solver_log = false;
    bool cgal_verify = true;
    std::size_t threads = 0; // 0 lets OpenMP choose.
};

struct SegmentData {
    Point a;
    Point b;
    std::uint32_t polygon_id = 0;
    std::uint32_t ring_id = 0;
    double ring_arclength_begin = 0.0;
};

struct BoundaryPoint {
    Point point{0.0, 0.0};
    Point boundary_anchor{0.0, 0.0};
    Point free_space_normal{0.0, 0.0};
    std::uint32_t polygon_id = 0;
    std::uint32_t ring_id = 0;
    double arclength = 0.0;
    double weight = 0.0;
    bool is_vertex = false;
    bool is_candidate = false;
    bool offset_from_boundary = false;
    // For candidates constructed directly on a source edge (currently the
    // designated incident-edge slides and optional subdivision points), this
    // stores the canonical edge parameter used by the exact verifier.
    bool has_canonical_edge_parameter = false;
    std::uint32_t canonical_edge_segment =
        std::numeric_limits<std::uint32_t>::max();
    double canonical_edge_parameter = 0.0;
    bool is_incident_edge_slide = false;
    std::array<std::uint32_t, 2> incident_segments{
        std::numeric_limits<std::uint32_t>::max(),
        std::numeric_limits<std::uint32_t>::max()
    };
};

struct RingData {
    std::vector<Point> vertices;
    std::vector<std::uint32_t> segment_ids;
    double perimeter = 0.0;
};

struct PolygonData {
    std::uint32_t internal_id = 0;
    std::int64_t source_fid = -1;
    std::vector<RingData> rings;
    std::vector<std::uint32_t> sample_ids;
    double perimeter = 0.0;
    OGRGeometryPtr geometry;
};

struct Scene {
    std::vector<PolygonData> polygons;
    std::vector<SegmentData> segments;
    std::vector<BoundaryPoint> samples;
    std::vector<std::uint32_t> candidates; // candidate index -> sample id
    PointRTree point_tree;
    PolygonRTree polygon_tree;
    Box data_bounds;
    bool has_data_bounds = false;
    OGRSpatialReferencePtr srs;
};

struct VisibilityConfig {
    double bbox_padding = 20.0;
};

[[nodiscard]] double x(const Point& p) { return bg::get<0>(p); }
[[nodiscard]] double y(const Point& p) { return bg::get<1>(p); }
[[nodiscard]] Point make_point(double px, double py) { return Point(px, py); }
[[nodiscard]] Point subtract(const Point& a, const Point& b) { return make_point(x(a) - x(b), y(a) - y(b)); }
[[nodiscard]] double cross(const Point& a, const Point& b) { return x(a) * y(b) - y(a) * x(b); }
[[nodiscard]] double dot(const Point& a, const Point& b) { return x(a) * x(b) + y(a) * y(b); }
[[nodiscard]] double squared_distance(const Point& a, const Point& b) {
    const double dx = x(a) - x(b);
    const double dy = y(a) - y(b);
    return dx * dx + dy * dy;
}
[[nodiscard]] double distance(const Point& a, const Point& b) { return std::sqrt(squared_distance(a, b)); }

[[nodiscard]] double normalize_angle(double angle) {
    angle = std::fmod(angle, TWO_PI);
    if (angle < 0.0) angle += TWO_PI;
    return angle;
}

[[nodiscard]] std::string usage() {
    return R"USAGE(
Usage:
  gis_cup_visibility --input buildings.geojson --k 10 --t 0.8 [options]

Required:
  --input PATH                  Input polygon GeoJSON/vector file.
  --k INT                       Final number of selected boundary points.
  --t FLOAT                     Final visible-boundary threshold in [0,1].
  --m FLOAT                     Curve exponent in t*(l/k)^m (default 1.0).
  --search-m                    Ternary-search m and rerun the best value with QGIS output.
  --m-min FLOAT                 Lower m search bound (default 0.5).
  --m-max FLOAT                 Upper m search bound (default 2.0).
  --m-iterations INT            Ternary rounds; up to two new runs per round (default 3).
  --m-workers INT               Concurrent m evaluations; useful maximum is 2 (default 2).

Geometry:
  --candidate-subdivisions INT  Equal parts per interval between consecutive visibility candidates
                                 on each original edge (default 10; 1 adds no interior points).
  --vertex-visibility-distance M Keep visibility-polygon candidate vertices within M metres of their
                                 generating polygon vertex; 0 = unlimited (default 0).
  --boundary-spacing METERS     Deprecated compatibility option; ignored.
  --candidate-spacing METERS    Deprecated compatibility option; ignored.
  --bbox-padding METERS         Padding around the data-domain visibility box (default 20).
  --boundary-epsilon METERS     Slide only the two incident-edge visibility vertices
                                 along the boundary edge they land on (default 1e-6).

Local search:
  --max-swap-passes INT         Accepted best swaps per cardinality; 0 means until local optimum.
  --swap-epsilon FLOAT          Required tie-break improvement in visible metres (default 1e-8).
  --multi-swap-max-new A        Consider exchanges introducing at most A new guards (default 3).
  --multi-swap-every B          Run the restricted A-swap phase every Bth add round (default 1).
  --multi-swap-max-rounds N     At most N restricted CP-SAT attempts per scheduled add round;
                                0 means continue until the first miss (default 0).
  --multi-swap-extra-count INT  Base anchor candidates for the restricted CP-SAT round (default 100).
  --multi-swap-time-limit SEC   Time limit for each restricted CP-SAT attempt (default 30).
  --multi-swap-partners-per-anchor N
                                Append up to N conditioned partners per anchor (default 1).
  --multi-swap-anchor-polygon-samples N
                                Sample N polygons seen by each anchor; 0 means all (default 8).
  --multi-swap-anchor-candidate-samples N
                                Evaluate at most N sampled partners per anchor; 0 means all
                                candidates in the sampled polygon lists (default 256).
  --multi-swap-anchor-seed INT  Deterministic anchor-subsampling seed (default 1).
  --no-multi-swap               Disable the intermediate restricted CP-SAT neighborhood.

Output / cache:
  --output-dir PATH             QGIS-ready GeoJSON snapshots (default gis_cup_output).
  --cache PATH                  Load/save compact candidate-to-sample visibility cache.
  --force-rebuild-cache         Ignore an existing cache.
  --cache-memory MODE           expanded (default, faster) or compressed (less RAM).
  --keep-duplicate-candidates   Disable exact visibility-row deduplication.
  --write-samples               Write all boundary samples once.
  --no-cgal-verify              Skip the final independent exact-kernel verifier.
  --progress-every INT          Visibility-build progress interval (default 100).
  --snapshot-every INT          Write QGIS snapshots every N accepted moves; 0 = final only (default 100).
  --timeline PATH               Optimization timeline CSV (default output-dir/optimization_timeline.csv).
  --version                     Print the source revision and exit.

Post-k pool polishing (OR-Tools CP-SAT):
  --polish-extra-fraction F     Add ceil(F*k) temporary pool points per round (default 0.05).
  --polish-extra-count INT      Exact temporary-point count; overrides the fraction when nonzero.
  --polish-max-rounds INT       Total CP-SAT rounds across alternation; 0 means until convergence.
  --polish-time-limit SECONDS   CP-SAT time limit per round; 0 means unlimited.
  --polish-relative-gap FLOAT   Relative CP-SAT objective gap (default 0).
  --polish-mip-gap FLOAT        Deprecated alias for --polish-relative-gap.
  --polish-workers INT          CP-SAT workers; 0 inherits --threads, then all cores.
  --polish-solver-log           Show CP-SAT search output.
  --no-pool-polish              Disable the post-k pool CP-SAT phase.

  --threads INT                 OpenMP workers; also CP-SAT fallback worker count.
  --help                        Show this help.
)USAGE";
}

template <typename T>
[[nodiscard]] T parse_number(const std::string& text, const std::string& name) {
    std::istringstream in(text);
    T value{};
    in >> value;
    if (!in || !in.eof()) {
        throw std::runtime_error("Invalid value for " + name + ": " + text);
    }
    return value;
}

[[nodiscard]] Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto value = [&](const std::string& name) -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("Missing value after " + name);
            return argv[++i];
        };

        if (arg == "--input") options.input = value(arg);
        else if (arg == "--output-dir") options.output_dir = value(arg);
        else if (arg == "--cache") options.cache_path = value(arg);
        else if (arg == "--timeline") options.timeline_path = value(arg);
        else if (arg == "--k") options.k = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--t") options.threshold = parse_number<double>(value(arg), arg);
        else if (arg == "--m") options.curve_exponent = parse_number<double>(value(arg), arg);
        else if (arg == "--search-m") options.search_curve_exponent = true;
        else if (arg == "--m-min") options.exponent_min = parse_number<double>(value(arg), arg);
        else if (arg == "--m-max") options.exponent_max = parse_number<double>(value(arg), arg);
        else if (arg == "--m-iterations") options.exponent_search_iterations = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--m-workers") options.exponent_workers = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--candidate-subdivisions") options.candidate_subdivisions = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--vertex-visibility-distance") options.vertex_visibility_distance = parse_number<double>(value(arg), arg);
        else if (arg == "--boundary-spacing") options.boundary_spacing = parse_number<double>(value(arg), arg);
        else if (arg == "--candidate-spacing") options.candidate_spacing = parse_number<double>(value(arg), arg);
        else if (arg == "--bbox-padding") options.bbox_padding = parse_number<double>(value(arg), arg);
        else if (arg == "--boundary-epsilon") options.boundary_epsilon = parse_number<double>(value(arg), arg);
        else if (arg == "--max-swap-passes") options.max_swap_passes = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--swap-epsilon") options.swap_improvement_epsilon = parse_number<double>(value(arg), arg);
        else if (arg == "--multi-swap-max-new") options.multi_swap_max_new = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-every") options.multi_swap_every = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-max-rounds") options.multi_swap_max_rounds = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-extra-count") options.multi_swap_extra_count = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-time-limit") options.multi_swap_time_limit = parse_number<double>(value(arg), arg);
        else if (arg == "--multi-swap-partners-per-anchor") options.multi_swap_partners_per_anchor = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-anchor-polygon-samples") options.multi_swap_anchor_polygon_samples = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-anchor-candidate-samples") options.multi_swap_anchor_candidate_samples = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--multi-swap-anchor-seed") options.multi_swap_anchor_seed = parse_number<std::uint64_t>(value(arg), arg);
        else if (arg == "--no-multi-swap") options.multi_swap_polish = false;
        else if (arg == "--progress-every") options.progress_every = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--snapshot-every") options.snapshot_every = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--polish-extra-fraction") options.polish_extra_fraction = parse_number<double>(value(arg), arg);
        else if (arg == "--polish-extra-count") options.polish_extra_count = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--polish-max-rounds") options.polish_max_rounds = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--polish-time-limit") options.polish_time_limit = parse_number<double>(value(arg), arg);
        else if (arg == "--polish-relative-gap" || arg == "--polish-mip-gap") {
            options.polish_mip_gap = parse_number<double>(value(arg), arg);
        }
        else if (arg == "--polish-workers") options.polish_workers = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--polish-solver-log") options.polish_solver_log = true;
        else if (arg == "--no-pool-polish") options.pool_polish = false;
        else if (arg == "--threads") options.threads = parse_number<std::size_t>(value(arg), arg);
        else if (arg == "--write-samples") options.write_samples = true;
        else if (arg == "--no-cgal-verify") options.cgal_verify = false;
        else if (arg == "--force-rebuild-cache") options.force_rebuild_cache = true;
        else if (arg == "--cache-memory") {
            const std::string mode = value(arg);
            if (mode == "expanded") options.expand_cache_in_memory = true;
            else if (mode == "compressed") options.expand_cache_in_memory = false;
            else throw std::runtime_error("--cache-memory must be expanded or compressed");
        }
        else if (arg == "--keep-duplicate-candidates") options.deduplicate_candidates = false;
        else if (arg == "--version") {
            std::cout << "gis_cup_visibility " << PROGRAM_REVISION << '\n';
            std::exit(0);
        }
        else if (arg == "--help" || arg == "-h") {
            std::cout << usage();
            std::exit(0);
        } else {
            throw std::runtime_error("Unknown option: " + arg);
        }
    }

    if (options.input.empty()) throw std::runtime_error("--input is required");
    if (options.k == 0) throw std::runtime_error("--k must be positive");
    if (!(options.threshold >= 0.0 && options.threshold <= 1.0)) throw std::runtime_error("--t must be in [0,1]");
    if (!(options.curve_exponent > 0.0) || !std::isfinite(options.curve_exponent)) throw std::runtime_error("--m must be finite and positive");
    if (!(options.exponent_min > 0.0) || !(options.exponent_max > options.exponent_min) ||
        !std::isfinite(options.exponent_min) || !std::isfinite(options.exponent_max)) {
        throw std::runtime_error("Require 0 < --m-min < --m-max");
    }
    if (options.exponent_search_iterations == 0) throw std::runtime_error("--m-iterations must be positive");
    if (options.exponent_workers == 0) throw std::runtime_error("--m-workers must be positive");
    if (options.candidate_subdivisions == 0) {
        throw std::runtime_error("--candidate-subdivisions must be positive");
    }
    if (!(options.vertex_visibility_distance >= 0.0) ||
        !std::isfinite(options.vertex_visibility_distance)) {
        throw std::runtime_error("--vertex-visibility-distance must be finite and nonnegative");
    }
    if (options.boundary_spacing <= 0.0) throw std::runtime_error("--boundary-spacing must be positive");
    if (options.candidate_spacing <= 0.0) throw std::runtime_error("--candidate-spacing must be positive");
    if (options.bbox_padding <= 0.0) throw std::runtime_error("--bbox-padding must be positive");
    if (!(options.boundary_epsilon >= 0.0) || !std::isfinite(options.boundary_epsilon)) {
        throw std::runtime_error("--boundary-epsilon must be finite and nonnegative");
    }
    if (options.swap_improvement_epsilon < 0.0) throw std::runtime_error("--swap-epsilon must be nonnegative");
    if (options.multi_swap_polish && options.multi_swap_max_new == 0) {
        throw std::runtime_error("--multi-swap-max-new must be positive unless --no-multi-swap is used");
    }
    if (options.multi_swap_every == 0) {
        throw std::runtime_error("--multi-swap-every must be positive");
    }
    if (options.multi_swap_polish && options.multi_swap_extra_count == 0) {
        throw std::runtime_error("--multi-swap-extra-count must be positive unless --no-multi-swap is used");
    }
    if (!(options.multi_swap_time_limit >= 0.0) || !std::isfinite(options.multi_swap_time_limit)) {
        throw std::runtime_error("--multi-swap-time-limit must be finite and nonnegative");
    }
    if (!(options.polish_extra_fraction >= 0.0) || !std::isfinite(options.polish_extra_fraction)) {
        throw std::runtime_error("--polish-extra-fraction must be finite and nonnegative");
    }
    if (!(options.polish_time_limit >= 0.0) || !std::isfinite(options.polish_time_limit)) {
        throw std::runtime_error("--polish-time-limit must be finite and nonnegative");
    }
    if (!(options.polish_mip_gap >= 0.0) || !std::isfinite(options.polish_mip_gap)) {
        throw std::runtime_error("--polish-relative-gap/--polish-mip-gap must be finite and nonnegative");
    }
    if (options.timeline_path.empty()) {
        options.timeline_path = options.output_dir / "optimization_timeline.csv";
    }
    return options;
}

[[nodiscard]] Box box_around(const Point& p, double radius) {
    return Box(make_point(x(p) - radius, y(p) - radius), make_point(x(p) + radius, y(p) + radius));
}

[[nodiscard]] Box segment_box(const Point& a, const Point& b) {
    return Box(make_point(std::min(x(a), x(b)), std::min(y(a), y(b))),
               make_point(std::max(x(a), x(b)), std::max(y(a), y(b))));
}

[[nodiscard]] double point_segment_distance(const Point& p, const Point& a, const Point& b) {
    const Point ab = subtract(b, a);
    const Point ap = subtract(p, a);
    const double denom = dot(ab, ab);
    if (denom <= 1e-20) return distance(p, a);
    const double t = std::clamp(dot(ap, ab) / denom, 0.0, 1.0);
    const Point projection = make_point(x(a) + t * x(ab), y(a) + t * y(ab));
    return distance(p, projection);
}

[[nodiscard]] bool nearly_equal(double a, double b, double eps = 1e-8) {
    return std::abs(a - b) <= eps;
}

[[nodiscard]] std::optional<double> ray_segment_distance(
    const Point& origin,
    double angle,
    const SegmentData& segment,
    double max_range)
{
    const Point direction = make_point(std::cos(angle), std::sin(angle));
    const Point s = subtract(segment.b, segment.a);
    const Point ao = subtract(segment.a, origin);
    const double denom = cross(direction, s);
    constexpr double eps = 1e-11;
    if (std::abs(denom) < eps) return std::nullopt;

    const double t = cross(ao, s) / denom;
    const double u = cross(ao, direction) / denom;
    if (t > 1e-7 && t <= max_range && u >= -1e-10 && u <= 1.0 + 1e-10) return t;
    return std::nullopt;
}

[[nodiscard]] bool point_on_segment(const Point& p, const Point& a, const Point& b, double eps = 1e-8) {
    if (point_segment_distance(p, a, b) > eps) return false;
    return x(p) >= std::min(x(a), x(b)) - eps && x(p) <= std::max(x(a), x(b)) + eps &&
           y(p) >= std::min(y(a), y(b)) - eps && y(p) <= std::max(y(a), y(b)) + eps;
}

[[nodiscard]] bool point_in_polygon_interior(const PolygonData& polygon, const Point& p) {
    bool inside = false;
    for (const RingData& ring : polygon.rings) {
        if (ring.vertices.size() < 3) continue;
        for (std::size_t i = 0, j = ring.vertices.size() - 1; i < ring.vertices.size(); j = i++) {
            const Point& a = ring.vertices[j];
            const Point& b = ring.vertices[i];
            if (point_on_segment(p, a, b)) return false;
            const bool crosses = ((y(a) > y(p)) != (y(b) > y(p))) &&
                (x(p) < (x(b) - x(a)) * (y(p) - y(a)) / (y(b) - y(a)) + x(a));
            if (crosses) inside = !inside;
        }
    }
    return inside;
}

[[nodiscard]] bool segment_blocks_line(
    const Point& source,
    const Point& target,
    const SegmentData& obstacle)
{
    const Point r = subtract(target, source);
    const Point s = subtract(obstacle.b, obstacle.a);
    const Point ao = subtract(obstacle.a, source);
    const double denom = cross(r, s);
    constexpr double eps = 1e-10;

    if (std::abs(denom) < eps) {
        // Collinear overlap is treated as visible along a boundary; this is intentional.
        return false;
    }

    const double t = cross(ao, s) / denom;
    const double u = cross(ao, r) / denom;
    // Ignore source/target touches and grazing an obstacle endpoint.
    return t > 1e-8 && t < 1.0 - 1e-8 && u > 1e-8 && u < 1.0 - 1e-8;
}

void add_ring_segments(
    Scene& scene,
    RingData& ring,
    std::uint32_t polygon_id,
    std::uint32_t ring_id)
{
    const std::size_t n = ring.vertices.size();
    ring.segment_ids.reserve(n);
    double arclength = 0.0;
    for (std::size_t i = 0; i < n; ++i) {
        const Point& a = ring.vertices[i];
        const Point& b = ring.vertices[(i + 1) % n];
        const auto sid = static_cast<std::uint32_t>(scene.segments.size());
        scene.segments.push_back({a, b, polygon_id, ring_id, arclength});
        ring.segment_ids.push_back(sid);
        const double length = distance(a, b);
        ring.perimeter += length;
        arclength += length;
    }
}

[[nodiscard]] RingData ring_from_ogr(const OGRLinearRing* ogr_ring) {
    RingData ring;
    if (ogr_ring == nullptr) return ring;
    int n = ogr_ring->getNumPoints();
    if (n >= 2 && nearly_equal(ogr_ring->getX(0), ogr_ring->getX(n - 1)) &&
        nearly_equal(ogr_ring->getY(0), ogr_ring->getY(n - 1))) {
        --n;
    }
    ring.vertices.reserve(static_cast<std::size_t>(std::max(n, 0)));
    for (int i = 0; i < n; ++i) {
        ring.vertices.emplace_back(ogr_ring->getX(i), ogr_ring->getY(i));
    }
    return ring;
}

void append_polygon(Scene& scene, const OGRPolygon& ogr_polygon, std::int64_t source_fid) {
    PolygonData polygon;
    polygon.internal_id = static_cast<std::uint32_t>(scene.polygons.size());
    polygon.source_fid = source_fid;
    polygon.geometry.reset(ogr_polygon.clone());

    if (const OGRLinearRing* exterior = ogr_polygon.getExteriorRing()) {
        polygon.rings.push_back(ring_from_ogr(exterior));
    }
    for (int h = 0; h < ogr_polygon.getNumInteriorRings(); ++h) {
        polygon.rings.push_back(ring_from_ogr(ogr_polygon.getInteriorRing(h)));
    }

    for (std::size_t ring_id = 0; ring_id < polygon.rings.size(); ++ring_id) {
        RingData& ring = polygon.rings[ring_id];
        if (ring.vertices.size() < 3) continue;
        add_ring_segments(
            scene, ring, polygon.internal_id, static_cast<std::uint32_t>(ring_id));
        polygon.perimeter += ring.perimeter;
    }
    scene.polygons.push_back(std::move(polygon));
}

using SegmentBreakpoints = std::vector<std::vector<double>>;

[[nodiscard]] std::uint64_t exact_double_key(double value) {
    // Canonicalize signed zero, but otherwise preserve the exact binary value.
    if (value == 0.0) value = 0.0;
    return std::bit_cast<std::uint64_t>(value);
}

[[nodiscard]] bool exact_point_equal(const Point& a, const Point& b) {
    return exact_double_key(x(a)) == exact_double_key(x(b)) &&
           exact_double_key(y(a)) == exact_double_key(y(b));
}

[[nodiscard]] Point interpolate_segment(const SegmentData& segment, double t) {
    t = std::clamp(t, 0.0, 1.0);
    return make_point(
        x(segment.a) + t * (x(segment.b) - x(segment.a)),
        y(segment.a) + t * (y(segment.b) - y(segment.a)));
}

[[nodiscard]] VerifierPoint verifier_point(const Point& point) {
    return VerifierPoint(x(point), y(point));
}

[[nodiscard]] VerifierFT exact_segment_parameter_from_point(
    const SegmentData& segment,
    const Point& point)
{
    const VerifierPoint a = verifier_point(segment.a);
    const VerifierPoint b = verifier_point(segment.b);
    const VerifierPoint p = verifier_point(point);
    const VerifierFT dx = b.x() - a.x();
    const VerifierFT dy = b.y() - a.y();
    if (CGAL::abs(dx) >= CGAL::abs(dy)) {
        if (dx == VerifierFT(0)) return VerifierFT(0);
        return (p.x() - a.x()) / dx;
    }
    if (dy == VerifierFT(0)) return VerifierFT(0);
    return (p.y() - a.y()) / dy;
}

[[nodiscard]] VerifierFT exact_ray_segment_parameter(
    const SegmentData& segment,
    const Point& query,
    const Point& ray_witness,
    const Point& approximate_intersection)
{
    const VerifierPoint a = verifier_point(segment.a);
    const VerifierPoint b = verifier_point(segment.b);
    const VerifierPoint q = verifier_point(query);
    const VerifierPoint w = verifier_point(ray_witness);
    const VerifierFT sx = b.x() - a.x();
    const VerifierFT sy = b.y() - a.y();
    const VerifierFT rx = w.x() - q.x();
    const VerifierFT ry = w.y() - q.y();
    const VerifierFT denominator = sx * ry - sy * rx;

    if (denominator != VerifierFT(0)) {
        const VerifierFT qax = q.x() - a.x();
        const VerifierFT qay = q.y() - a.y();
        const VerifierFT parameter = (qax * ry - qay * rx) / denominator;
        if (parameter >= VerifierFT(0) && parameter <= VerifierFT(1)) {
            return parameter;
        }
    }

    // A zero denominator means the ray and edge are collinear, so their
    // intersection is not a unique mathematical point. Do not merge such
    // candidates by topology; preserve the emitted point's exact-double edge
    // parameter instead. The two deliberately handled incident-edge vertices
    // already carry explicit endpoint parameters from their source fragments.
    VerifierFT parameter = exact_segment_parameter_from_point(segment, approximate_intersection);
    if (parameter < VerifierFT(0)) parameter = VerifierFT(0);
    if (parameter > VerifierFT(1)) parameter = VerifierFT(1);
    return parameter;
}

struct ParameterInterval {
    long double lo = 0.0L;
    long double hi = 0.0L;
};

[[nodiscard]] long double outward_down(long double value) {
    return std::isfinite(value)
        ? std::nextafter(value, -std::numeric_limits<long double>::infinity())
        : value;
}

[[nodiscard]] long double outward_up(long double value) {
    return std::isfinite(value)
        ? std::nextafter(value, std::numeric_limits<long double>::infinity())
        : value;
}

[[nodiscard]] ParameterInterval interval_value(double value) {
    const long double exact = static_cast<long double>(value);
    return {exact, exact};
}

[[nodiscard]] ParameterInterval interval_add(
    const ParameterInterval& a,
    const ParameterInterval& b)
{
    return {outward_down(a.lo + b.lo), outward_up(a.hi + b.hi)};
}

[[nodiscard]] ParameterInterval interval_subtract(
    const ParameterInterval& a,
    const ParameterInterval& b)
{
    return {outward_down(a.lo - b.hi), outward_up(a.hi - b.lo)};
}

[[nodiscard]] ParameterInterval interval_multiply(
    const ParameterInterval& a,
    const ParameterInterval& b)
{
    const std::array<long double, 4> products{
        a.lo * b.lo,
        a.lo * b.hi,
        a.hi * b.lo,
        a.hi * b.hi
    };
    const auto bounds = std::minmax_element(products.begin(), products.end());
    return {outward_down(*bounds.first), outward_up(*bounds.second)};
}

[[nodiscard]] ParameterInterval interval_divide(
    const ParameterInterval& numerator,
    const ParameterInterval& denominator)
{
    if (denominator.lo <= 0.0L && denominator.hi >= 0.0L) {
        return {
            -std::numeric_limits<long double>::infinity(),
            std::numeric_limits<long double>::infinity()
        };
    }
    const std::array<long double, 4> quotients{
        numerator.lo / denominator.lo,
        numerator.lo / denominator.hi,
        numerator.hi / denominator.lo,
        numerator.hi / denominator.hi
    };
    const auto bounds = std::minmax_element(quotients.begin(), quotients.end());
    return {outward_down(*bounds.first), outward_up(*bounds.second)};
}

[[nodiscard]] ParameterInterval interval_absolute(const ParameterInterval& value) {
    if (value.lo >= 0.0L) return value;
    if (value.hi <= 0.0L) return {-value.hi, -value.lo};
    return {0.0L, std::max(-value.lo, value.hi)};
}

[[nodiscard]] ParameterInterval clamp_parameter_interval(ParameterInterval value) {
    value.lo = std::max(0.0L, value.lo);
    value.hi = std::min(1.0L, value.hi);
    if (value.lo > value.hi) {
        const long double midpoint = std::clamp(
            0.5L * (value.lo + value.hi), 0.0L, 1.0L);
        return {midpoint, midpoint};
    }
    return value;
}

[[nodiscard]] ParameterInterval segment_parameter_interval_from_point(
    const SegmentData& segment,
    const Point& point)
{
    const ParameterInterval ax = interval_value(x(segment.a));
    const ParameterInterval ay = interval_value(y(segment.a));
    const ParameterInterval bx = interval_value(x(segment.b));
    const ParameterInterval by = interval_value(y(segment.b));
    const ParameterInterval px = interval_value(x(point));
    const ParameterInterval py = interval_value(y(point));
    const ParameterInterval dx = interval_subtract(bx, ax);
    const ParameterInterval dy = interval_subtract(by, ay);

    const ParameterInterval abs_dx = interval_absolute(dx);
    const ParameterInterval abs_dy = interval_absolute(dy);
    const ParameterInterval parameter_x =
        clamp_parameter_interval(interval_divide(interval_subtract(px, ax), dx));
    const ParameterInterval parameter_y =
        clamp_parameter_interval(interval_divide(interval_subtract(py, ay), dy));
    if (abs_dx.lo >= abs_dy.hi) return parameter_x;
    if (abs_dy.lo > abs_dx.hi) return parameter_y;
    // The exact implementation chooses the dominant coordinate.  When the
    // interval comparison cannot certify which one wins, retain both possible
    // results; this may trigger extra exact probes but cannot miss an equality.
    return {
        std::min(parameter_x.lo, parameter_y.lo),
        std::max(parameter_x.hi, parameter_y.hi)
    };
}

[[nodiscard]] ParameterInterval ray_segment_parameter_interval(
    const SegmentData& segment,
    const Point& query,
    const Point& ray_witness,
    const Point& approximate_intersection)
{
    const ParameterInterval ax = interval_value(x(segment.a));
    const ParameterInterval ay = interval_value(y(segment.a));
    const ParameterInterval bx = interval_value(x(segment.b));
    const ParameterInterval by = interval_value(y(segment.b));
    const ParameterInterval qx = interval_value(x(query));
    const ParameterInterval qy = interval_value(y(query));
    const ParameterInterval wx = interval_value(x(ray_witness));
    const ParameterInterval wy = interval_value(y(ray_witness));

    const ParameterInterval sx = interval_subtract(bx, ax);
    const ParameterInterval sy = interval_subtract(by, ay);
    const ParameterInterval rx = interval_subtract(wx, qx);
    const ParameterInterval ry = interval_subtract(wy, qy);
    const ParameterInterval qax = interval_subtract(qx, ax);
    const ParameterInterval qay = interval_subtract(qy, ay);
    const ParameterInterval numerator = interval_subtract(
        interval_multiply(qax, ry), interval_multiply(qay, rx));
    const ParameterInterval denominator = interval_subtract(
        interval_multiply(sx, ry), interval_multiply(sy, rx));
    const ParameterInterval fallback =
        segment_parameter_interval_from_point(segment, approximate_intersection);

    if (denominator.lo <= 0.0L && denominator.hi >= 0.0L) {
        // The exact implementation either accepts a ray parameter in [0,1]
        // or falls back to a clamped point parameter, so [0,1] contains both
        // branches even when interval arithmetic cannot certify the sign.
        return {0.0L, 1.0L};
    }

    const ParameterInterval ray = interval_divide(numerator, denominator);
    if (ray.lo >= 0.0L && ray.hi <= 1.0L) return ray;
    if (ray.hi < 0.0L || ray.lo > 1.0L) return fallback;
    return clamp_parameter_interval({
        std::min(ray.lo, fallback.lo),
        std::max(ray.hi, fallback.hi)
    });
}

[[nodiscard]] double signed_ring_area_twice(const RingData& ring) {
    double area = 0.0;
    for (std::size_t i = 0; i < ring.vertices.size(); ++i) {
        const Point& a = ring.vertices[i];
        const Point& b = ring.vertices[(i + 1) % ring.vertices.size()];
        area += x(a) * y(b) - x(b) * y(a);
    }
    return area;
}

[[nodiscard]] Point free_space_unit_normal(
    const Scene& scene,
    std::uint32_t segment_id)
{
    const SegmentData& segment = scene.segments.at(segment_id);
    const Point tangent = subtract(segment.b, segment.a);
    const double length = std::hypot(x(tangent), y(tangent));
    if (!(length > 0.0)) return make_point(0.0, 0.0);

    const Point left = make_point(-y(tangent) / length, x(tangent) / length);
    const RingData& ring = scene.polygons.at(segment.polygon_id).rings.at(segment.ring_id);
    const bool ring_interior_is_left = signed_ring_area_twice(ring) > 0.0;
    Point ring_interior = ring_interior_is_left
        ? left
        : make_point(-x(left), -y(left));

    // Exterior-ring interiors are obstacle space, while interior-ring interiors
    // are holes and therefore free space.
    if (segment.ring_id == 0) {
        ring_interior = make_point(-x(ring_interior), -y(ring_interior));
    }
    return ring_interior;
}

void normalize_breakpoints_exact(
    const Scene& scene,
    SegmentBreakpoints& breakpoints)
{
    if (breakpoints.size() != scene.segments.size()) {
        throw std::runtime_error("Breakpoint array does not match source-segment count");
    }
    for (auto& values : breakpoints) {
        values.push_back(0.0);
        values.push_back(1.0);
        for (double& value : values) {
            value = std::clamp(value, 0.0, 1.0);
            if (value == 0.0) value = 0.0; // Canonicalize signed zero.
        }
        std::sort(values.begin(), values.end());
        values.erase(std::unique(values.begin(), values.end()), values.end());
    }
}

void clear_boundary_discretization(Scene& scene) {
    scene.samples.clear();
    scene.candidates.clear();
    for (PolygonData& polygon : scene.polygons) polygon.sample_ids.clear();
}

void add_incident_segment(BoundaryPoint& point, std::uint32_t segment_id) {
    if (point.incident_segments[0] == segment_id ||
        point.incident_segments[1] == segment_id) return;
    if (point.incident_segments[0] == std::numeric_limits<std::uint32_t>::max()) {
        point.incident_segments[0] = segment_id;
        return;
    }
    if (point.incident_segments[1] == std::numeric_limits<std::uint32_t>::max()) {
        point.incident_segments[1] = segment_id;
        return;
    }
    throw std::runtime_error("A generated boundary candidate belongs to more than two ring edges");
}

void append_candidate_sample(Scene& scene, BoundaryPoint candidate) {
    if (scene.samples.size() >=
        static_cast<std::size_t>(std::numeric_limits<std::uint32_t>::max())) {
        throw std::runtime_error("Too many generated candidates for 32-bit sample IDs");
    }
    candidate.is_candidate = true;
    candidate.weight = 0.0;
    const auto sample_id = static_cast<std::uint32_t>(scene.samples.size());
    scene.samples.push_back(std::move(candidate));
    scene.candidates.push_back(sample_id);
}

struct CandidateSubdivisionStats {
    std::size_t source_intervals = 0;
    std::size_t added_candidates = 0;
};

struct ThreeStagePipelineAudit {
    std::size_t seed_vertices = 0;
    std::size_t visibility_vertex_candidates = 0;
    std::size_t subdivision_candidates = 0;
    std::size_t final_candidates = 0;
    std::size_t incident_edge_slides = 0;
    std::size_t second_round_boundary_endpoints = 0;
    std::size_t coverage_intervals = 0;
};

void validate_final_boundary_candidates(
    const Scene& scene,
    std::size_t expected_count)
{
    if (scene.candidates.size() != expected_count) {
        throw std::runtime_error(
            "Three-stage pipeline invariant failed: unexpected final candidate count");
    }
    if (scene.samples.size() != scene.candidates.size()) {
        throw std::runtime_error(
            "Three-stage pipeline invariant failed: non-candidate samples exist before coverage discretization");
    }

    std::vector<bool> seen(scene.samples.size(), false);
    for (const std::uint32_t sample_id : scene.candidates) {
        if (sample_id >= scene.samples.size()) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: candidate ID is out of range");
        }
        if (seen[sample_id]) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: duplicate final candidate ID");
        }
        seen[sample_id] = true;

        const BoundaryPoint& candidate = scene.samples[sample_id];
        if (!candidate.is_candidate || candidate.weight != 0.0) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: final guard candidate has coverage weight");
        }
        if (candidate.incident_segments[0] ==
            std::numeric_limits<std::uint32_t>::max()) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: final guard candidate has no boundary edge");
        }
        if (candidate.offset_from_boundary) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: a final guard candidate left the polygon boundary");
        }
    }
}

void validate_coverage_only_append(
    const Scene& scene,
    const std::vector<std::uint32_t>& final_candidate_ids,
    std::size_t first_coverage_sample)
{
    if (scene.candidates != final_candidate_ids) {
        throw std::runtime_error(
            "Three-stage pipeline invariant failed: second-round visibility recursively changed the candidate set");
    }
    if (first_coverage_sample != final_candidate_ids.size()) {
        throw std::runtime_error(
            "Three-stage pipeline invariant failed: candidate/sample layout changed before coverage discretization");
    }
    for (std::size_t sample_id = first_coverage_sample;
         sample_id < scene.samples.size();
         ++sample_id) {
        const BoundaryPoint& sample = scene.samples[sample_id];
        if (sample.is_candidate || sample.is_incident_edge_slide ||
            sample.offset_from_boundary || !(sample.weight > 0.0)) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: second-round visibility produced a recursive or perturbed candidate");
        }
    }
}

[[nodiscard]] double candidate_parameter_on_segment(
    const Scene& scene,
    const BoundaryPoint& candidate,
    std::uint32_t segment_id)
{
    if (candidate.has_canonical_edge_parameter &&
        candidate.canonical_edge_segment == segment_id) {
        return std::clamp(candidate.canonical_edge_parameter, 0.0, 1.0);
    }

    const SegmentData& segment = scene.segments.at(segment_id);
    if (exact_point_equal(candidate.point, segment.a)) return 0.0;
    if (exact_point_equal(candidate.point, segment.b)) return 1.0;

    const Point direction = subtract(segment.b, segment.a);
    const double denominator = dot(direction, direction);
    if (!(denominator > 0.0)) return 0.0;
    return std::clamp(
        dot(subtract(candidate.point, segment.a), direction) / denominator,
        0.0,
        1.0);
}

CandidateSubdivisionStats append_candidate_subdivisions(
    Scene& scene,
    std::size_t subdivisions)
{
    if (subdivisions == 0) {
        throw std::runtime_error("Candidate subdivisions must be positive");
    }

    CandidateSubdivisionStats stats;
    std::vector<std::vector<double>> parameters(scene.segments.size());
    for (auto& values : parameters) {
        values.push_back(0.0);
        values.push_back(1.0);
    }

    // Snapshot the visibility-derived set. Newly inserted subdivision points do
    // not recursively create more intervals in this stage.
    const std::size_t base_candidate_count = scene.candidates.size();
    for (std::size_t candidate_index = 0;
         candidate_index < base_candidate_count;
         ++candidate_index) {
        const BoundaryPoint& candidate =
            scene.samples.at(scene.candidates.at(candidate_index));
        for (const std::uint32_t segment_id : candidate.incident_segments) {
            if (segment_id == std::numeric_limits<std::uint32_t>::max() ||
                segment_id >= scene.segments.size()) {
                continue;
            }
            parameters[segment_id].push_back(
                candidate_parameter_on_segment(scene, candidate, segment_id));
        }
    }

    for (std::size_t sid = 0; sid < scene.segments.size(); ++sid) {
        auto& values = parameters[sid];
        for (double& value : values) {
            value = std::clamp(value, 0.0, 1.0);
            if (value == 0.0) value = 0.0;
        }
        std::sort(values.begin(), values.end());
        values.erase(std::unique(values.begin(), values.end()), values.end());

        const SegmentData& segment = scene.segments[sid];
        const double segment_length = distance(segment.a, segment.b);
        if (!(segment_length > 0.0)) continue;

        for (std::size_t interval = 1; interval < values.size(); ++interval) {
            const double lo = values[interval - 1];
            const double hi = values[interval];
            if (!(hi > lo)) continue;
            ++stats.source_intervals;

            for (std::size_t part = 1; part < subdivisions; ++part) {
                const double fraction = static_cast<double>(part) /
                    static_cast<double>(subdivisions);
                const double parameter = lo + fraction * (hi - lo);
                // Tiny source intervals can round an interior construction back
                // to an endpoint. In that case the endpoint candidate already
                // exists, so do not create a binary duplicate.
                if (!(parameter > lo && parameter < hi)) continue;

                BoundaryPoint candidate;
                candidate.point = interpolate_segment(segment, parameter);
                candidate.boundary_anchor = candidate.point;
                candidate.free_space_normal = free_space_unit_normal(
                    scene, static_cast<std::uint32_t>(sid));
                candidate.polygon_id = segment.polygon_id;
                candidate.ring_id = segment.ring_id;
                candidate.arclength = segment.ring_arclength_begin +
                    parameter * segment_length;
                candidate.is_vertex = false;
                candidate.has_canonical_edge_parameter = true;
                candidate.canonical_edge_segment = static_cast<std::uint32_t>(sid);
                candidate.canonical_edge_parameter = parameter;
                candidate.is_incident_edge_slide = false;
                candidate.incident_segments[0] = static_cast<std::uint32_t>(sid);
                append_candidate_sample(scene, std::move(candidate));
                ++stats.added_candidates;
            }
        }
    }
    return stats;
}

void build_vertex_candidates(Scene& scene) {
    clear_boundary_discretization(scene);
    for (const PolygonData& polygon : scene.polygons) {
        for (std::size_t ring_id = 0; ring_id < polygon.rings.size(); ++ring_id) {
            const RingData& ring = polygon.rings[ring_id];
            const std::size_t n = ring.vertices.size();
            if (n == 0 || ring.segment_ids.size() != n) continue;
            for (std::size_t i = 0; i < n; ++i) {
                BoundaryPoint candidate;
                candidate.point = ring.vertices[i];
                candidate.boundary_anchor = candidate.point;
                candidate.polygon_id = polygon.internal_id;
                candidate.ring_id = static_cast<std::uint32_t>(ring_id);
                candidate.arclength = scene.segments.at(ring.segment_ids[i]).ring_arclength_begin;
                candidate.is_vertex = true;
                candidate.incident_segments[0] = ring.segment_ids[(i + n - 1) % n];
                candidate.incident_segments[1] = ring.segment_ids[i];
                append_candidate_sample(scene, std::move(candidate));
            }
        }
    }
}

void append_coverage_samples_from_breakpoints(
    Scene& scene,
    SegmentBreakpoints breakpoints)
{
    normalize_breakpoints_exact(scene, breakpoints);
    for (PolygonData& polygon : scene.polygons) polygon.sample_ids.clear();

    double total_weight = 0.0;
    for (std::size_t sid = 0; sid < scene.segments.size(); ++sid) {
        const SegmentData& segment = scene.segments[sid];
        const double length = distance(segment.a, segment.b);
        if (length <= 0.0) continue;
        const auto& values = breakpoints[sid];
        for (std::size_t i = 1; i < values.size(); ++i) {
            const double lo = values[i - 1];
            const double hi = values[i];
            if (!(hi > lo)) continue;

            BoundaryPoint sample;
            sample.point = interpolate_segment(segment, 0.5 * (lo + hi));
            sample.boundary_anchor = sample.point;
            sample.polygon_id = segment.polygon_id;
            sample.ring_id = segment.ring_id;
            sample.arclength = segment.ring_arclength_begin + 0.5 * (lo + hi) * length;
            sample.weight = (hi - lo) * length;
            sample.is_vertex = false;
            sample.is_candidate = false;
            sample.incident_segments[0] = static_cast<std::uint32_t>(sid);

            if (scene.samples.size() >=
                static_cast<std::size_t>(std::numeric_limits<std::uint32_t>::max())) {
                throw std::runtime_error("Too many generated boundary intervals for 32-bit sample IDs");
            }
            const auto sample_id = static_cast<std::uint32_t>(scene.samples.size());
            scene.samples.push_back(sample);
            scene.polygons[segment.polygon_id].sample_ids.push_back(sample_id);
            total_weight += sample.weight;
        }
    }

    const double perimeter_total = std::accumulate(
        scene.polygons.begin(), scene.polygons.end(), 0.0,
        [](double sum, const PolygonData& polygon) { return sum + polygon.perimeter; });
    const double tolerance = std::max(1e-7, 1e-9 * perimeter_total);
    if (std::abs(total_weight - perimeter_total) > tolerance) {
        throw std::runtime_error(
            "Visibility-induced coverage intervals do not preserve total polygon perimeter");
    }
}

void rebuild_point_tree(Scene& scene) {
    std::vector<PointValue> values;
    values.reserve(scene.samples.size());
    for (std::uint32_t i = 0; i < scene.samples.size(); ++i) {
        values.emplace_back(scene.samples[i].point, i);
    }
    scene.point_tree = PointRTree(values.begin(), values.end());
}

[[nodiscard]] Scene load_scene(const Options& options) {
    GDALAllRegister();
    GDALDatasetPtr dataset(static_cast<GDALDataset*>(GDALOpenEx(
        options.input.string().c_str(), GDAL_OF_VECTOR | GDAL_OF_READONLY, nullptr, nullptr, nullptr)));
    if (!dataset) throw std::runtime_error("Could not open input: " + options.input.string());

    OGRLayer* layer = dataset->GetLayer(0);
    if (layer == nullptr) throw std::runtime_error("Input has no vector layer");

    Scene scene;
    if (const OGRSpatialReference* layer_srs = layer->GetSpatialRef()) scene.srs.reset(layer_srs->Clone());

    layer->ResetReading();
    while (OGRFeature* raw_feature = layer->GetNextFeature()) {
        OGRFeaturePtr feature(raw_feature);
        OGRGeometry* geometry = feature->GetGeometryRef();
        if (geometry == nullptr) continue;
        const auto type = wkbFlatten(geometry->getGeometryType());
        if (type == wkbPolygon) {
            append_polygon(scene, *geometry->toPolygon(), feature->GetFID());
        } else if (type == wkbMultiPolygon) {
            const auto* multi = geometry->toMultiPolygon();
            for (const auto* part : *multi) append_polygon(scene, *part, feature->GetFID());
        } else {
            std::cerr << "Skipping non-polygon feature FID " << feature->GetFID() << "\n";
        }
    }

    std::vector<PolygonValue> polygon_values;
    polygon_values.reserve(scene.polygons.size());
    bool first_bound = true;
    double min_x = 0.0, min_y = 0.0, max_x = 0.0, max_y = 0.0;
    for (const auto& polygon : scene.polygons) {
        OGREnvelope env{};
        polygon.geometry->getEnvelope(&env);
        const Box box(make_point(env.MinX, env.MinY), make_point(env.MaxX, env.MaxY));
        polygon_values.emplace_back(box, polygon.internal_id);
        if (first_bound) {
            min_x = env.MinX; min_y = env.MinY; max_x = env.MaxX; max_y = env.MaxY;
            first_bound = false;
        } else {
            min_x = std::min(min_x, env.MinX); min_y = std::min(min_y, env.MinY);
            max_x = std::max(max_x, env.MaxX); max_y = std::max(max_y, env.MaxY);
        }
    }
    scene.polygon_tree = PolygonRTree(polygon_values.begin(), polygon_values.end());
    if (!first_bound) {
        scene.data_bounds = Box(make_point(min_x, min_y), make_point(max_x, max_y));
        scene.has_data_bounds = true;
    }

    return scene;
}


struct CoordinateKey {
    std::int64_t xq = 0;
    std::int64_t yq = 0;
    bool operator==(const CoordinateKey&) const = default;
};

struct CoordinateKeyHash {
    std::size_t operator()(const CoordinateKey& key) const noexcept {
        const std::uint64_t a = static_cast<std::uint64_t>(key.xq);
        const std::uint64_t b = static_cast<std::uint64_t>(key.yq);
        return static_cast<std::size_t>((a * 0x9E3779B185EBCA87ULL) ^ (b + 0xC2B2AE3D27D4EB4FULL));
    }
};

[[nodiscard]] CoordinateKey coordinate_key(double px, double py) {
    constexpr double scale = 1e8;
    return {
        static_cast<std::int64_t>(std::llround(px * scale)),
        static_cast<std::int64_t>(std::llround(py * scale))
    };
}

class VisibilityEngine {
public:
    struct Interval {
        double lo = 0.0;
        double hi = 0.0;
        Point lo_ray_witness{0.0, 0.0};
        Point hi_ray_witness{0.0, 0.0};
        bool lo_has_witness = false;
        bool hi_has_witness = false;
    };

    struct QuerySeed {
        CdtFaceHandle start_face;
        Interval wedge;
    };

    struct QuerySite {
        CgalPoint query_point;
        std::vector<QuerySeed> seeds;
        bool interior_query = false;
    };

    struct WalkStats {
        std::uint64_t faces = 0;
        std::uint64_t portals = 0;
        std::uint64_t blockers = 0;
    };

    VisibilityEngine(const Scene& scene, VisibilityConfig config)
        : scene_(scene), config_(config)
    {
        if (!scene_.has_data_bounds) throw std::runtime_error("Cannot build visibility domain without data bounds");
        std::cout << "Building one global CDT from " << scene_.segments.size()
                  << " original footprint edges (boundary samples are not inserted)...\n" << std::flush;
        const auto t0 = std::chrono::steady_clock::now();
        build_triangulation();
        const double coordinate_scale = std::max({
            1.0,
            std::abs(x(domain_box_.min_corner())),
            std::abs(y(domain_box_.min_corner())),
            std::abs(x(domain_box_.max_corner())),
            std::abs(y(domain_box_.max_corner()))});
        numerical_point_epsilon_ = std::max(
            1e-10,
            128.0 * std::numeric_limits<double>::epsilon() * coordinate_scale);
        const double tri_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        std::cout << "CDT built: " << cdt_.number_of_vertices() << " vertices, "
                  << cdt_.number_of_faces() << " finite faces in " << tri_seconds << " s.\n";
        const auto t1 = std::chrono::steady_clock::now();
        index_static_edges_and_portals();
        refresh_scene_points();
        const double index_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - t1).count();
        std::cout << "CDT metadata/query sites indexed in " << index_seconds << " s.\n";
    }

    [[nodiscard]] const Box& domain_box() const { return domain_box_; }
    [[nodiscard]] std::size_t triangulation_vertices() const { return cdt_.number_of_vertices(); }
    [[nodiscard]] std::size_t triangulation_faces() const { return cdt_.number_of_faces(); }
    [[nodiscard]] double query_offset(std::size_t candidate_index) const {
        (void)query_sites_.at(candidate_index);
        const BoundaryPoint& sample =
            scene_.samples.at(scene_.candidates.at(candidate_index));
        return distance(sample.point, sample.boundary_anchor);
    }
    [[nodiscard]] Point query_point(std::size_t candidate_index) const {
        const CgalPoint& q = query_sites_.at(candidate_index).query_point;
        return Point(CGAL::to_double(q.x()), CGAL::to_double(q.y()));
    }

    void refresh_scene_points() {
        index_samples();
        index_query_sites();
    }

    struct BoundaryBreakpoint {
        std::uint32_t segment_id = 0;
        double parameter = 0.0;
    };

    struct CandidateVertex {
        Point boundary_point;
        std::uint32_t generating_segment = 0;
        // Provenance for lazy exact same-edge identity.  The visibility walk
        // stays entirely in the inexact kernel; an EPECK parameter is created
        // only when two approximate candidates on the same edge may coincide.
        Point ray_origin{0.0, 0.0};
        Point ray_witness{0.0, 0.0};
        bool has_ray_witness = false;
        bool slide_on_landing_edge = false;
    };

    [[nodiscard]] std::vector<CandidateVertex> compute_visibility_candidates(
        std::size_t candidate_index,
        double max_vertex_distance = 0.0,
        WalkStats* stats = nullptr) const
    {
        WalkOutput output = walk(candidate_index, true);
        if (stats != nullptr) *stats = output.stats;

        const Point generating_point =
            scene_.samples.at(scene_.candidates.at(candidate_index)).point;
        const bool distance_bounded = max_vertex_distance > 0.0;
        const double max_distance_squared = max_vertex_distance * max_vertex_distance;
        auto within_distance = [&](const Point& point) {
            return !distance_bounded ||
                squared_distance(generating_point, point) <= max_distance_squared + 1e-12;
        };

        std::vector<CandidateVertex> result;
        result.reserve(output.fragments.size() * 2);
        auto append = [&](const Point& point,
                          std::uint32_t sid,
                          const Point& ray_witness,
                          bool has_ray_witness,
                          bool wiggle) {
            if (exact_point_equal(point, generating_point) ||
                !within_distance(point)) return;
            result.push_back({
                point,
                sid,
                generating_point,
                ray_witness,
                has_ray_witness,
                wiggle});
        };

        for (const VisibleFragment& fragment : output.fragments) {
            if (fragment.source_segment < 0) continue;
            const auto sid = static_cast<std::uint32_t>(fragment.source_segment);
            if (fragment.query_incident_edge) {
                // This fragment was inserted explicitly for one of the two source
                // edges incident to the original vertex. Only its non-query end
                // is slid by epsilon along the next boundary edge it reaches.
                if (!exact_point_equal(fragment.a, generating_point)) {
                    append(fragment.a, sid, fragment.a_ray_witness,
                           fragment.a_has_ray_witness, true);
                }
                if (!exact_point_equal(fragment.b, generating_point)) {
                    append(fragment.b, sid, fragment.b_ray_witness,
                           fragment.b_has_ray_witness, true);
                }
            } else {
                append(fragment.a, sid, fragment.a_ray_witness,
                       fragment.a_has_ray_witness, false);
                append(fragment.b, sid, fragment.b_ray_witness,
                       fragment.b_has_ray_witness, false);
            }
        }
        return result;
    }

    [[nodiscard]] std::vector<BoundaryBreakpoint> compute_visibility_breakpoints(
        std::size_t candidate_index,
        double max_vertex_distance = 0.0,
        WalkStats* stats = nullptr) const
    {
        WalkOutput output = walk(candidate_index, true);
        if (stats != nullptr) *stats = output.stats;
        std::vector<BoundaryBreakpoint> result;
        result.reserve(output.fragments.size() * 2);

        const Point generating_point =
            scene_.samples.at(scene_.candidates.at(candidate_index)).point;
        const bool distance_bounded = max_vertex_distance > 0.0;
        const double max_distance_squared = max_vertex_distance * max_vertex_distance;
        auto within_distance = [&](const Point& point) {
            return !distance_bounded ||
                squared_distance(generating_point, point) <= max_distance_squared + 1e-12;
        };

        for (const VisibleFragment& fragment : output.fragments) {
            if (fragment.source_segment < 0) continue;
            const auto sid = static_cast<std::uint32_t>(fragment.source_segment);
            const SegmentData& source = scene_.segments.at(sid);
            const Point direction = subtract(source.b, source.a);
            const double denominator = dot(direction, direction);
            if (denominator <= 1e-20) continue;
            auto parameter = [&](const Point& point) {
                return std::clamp(
                    dot(subtract(point, source.a), direction) / denominator, 0.0, 1.0);
            };
            if (within_distance(fragment.a)) {
                result.push_back({sid, parameter(fragment.a)});
            }
            if (within_distance(fragment.b)) {
                result.push_back({sid, parameter(fragment.b)});
            }
        }
        return result;
    }

    [[nodiscard]] std::vector<std::uint32_t> compute(std::size_t candidate_index, WalkStats* stats = nullptr) const {
        WalkOutput output = walk(candidate_index, false);
        if (stats != nullptr) *stats = output.stats;
        return std::move(output.visible_samples);
    }

    [[nodiscard]] std::vector<Point> compute_visibility_polygon(std::size_t candidate_index) const {
        WalkOutput output = walk(candidate_index, true);
        const QuerySite& site = query_sites_.at(candidate_index);
        const Point q = query_point(candidate_index);
        constexpr double angular_epsilon = 1e-11;
        const double point_epsilon = numerical_point_epsilon_;
        const double point_epsilon_squared = point_epsilon * point_epsilon;

        // Merge the exact local free-space sectors. A sector crossing angle zero
        // is represented as one unwrapped interval [lo, hi + 2*pi]. For ordinary
        // footprint boundaries there is exactly one connected local free sector.
        std::vector<Interval> sectors;
        sectors.reserve(site.seeds.size());
        for (const QuerySeed& seed : site.seeds) sectors.push_back(seed.wedge);
        std::sort(sectors.begin(), sectors.end(), [](const Interval& a, const Interval& b) {
            if (a.lo != b.lo) return a.lo < b.lo;
            return a.hi < b.hi;
        });
        std::vector<Interval> merged;
        for (const Interval sector : sectors) {
            if (merged.empty() || sector.lo > merged.back().hi + angular_epsilon) {
                merged.push_back(sector);
            } else {
                merged.back().hi = std::max(merged.back().hi, sector.hi);
            }
        }
        if (merged.size() >= 2 &&
            merged.front().lo <= angular_epsilon &&
            merged.back().hi >= TWO_PI - angular_epsilon) {
            const Interval wrapped{
                merged.back().lo,
                merged.front().hi + TWO_PI};
            merged.pop_back();
            merged.erase(merged.begin());
            merged.push_back(wrapped);
        }
        if (merged.empty()) {
            throw std::runtime_error("Exact boundary query has no free angular sector");
        }

        // A touching/overlapping input can create several components meeting only
        // at the query point. The current GeoJSON layer stores Polygon rather than
        // MultiPolygon, so visualize the largest component. Coverage and breakpoint
        // computation still use every sector in walk().
        const Interval visible_sector = *std::max_element(
            merged.begin(), merged.end(), [](const Interval& a, const Interval& b) {
                return (a.hi - a.lo) < (b.hi - b.lo);
            });

        struct Event {
            double angle = 0.0;
            int kind = 0; // 0=end of previous fragment, 1=start of next fragment
            double distance2 = 0.0;
            Point point;
        };
        std::vector<Event> events;
        events.reserve(output.fragments.size() * 2);

        auto unwrap_angle = [&](double angle) -> std::optional<double> {
            angle = normalize_angle(angle);
            while (angle < visible_sector.lo - angular_epsilon) angle += TWO_PI;
            if (angle > visible_sector.hi + angular_epsilon) return std::nullopt;
            return std::clamp(angle, visible_sector.lo, visible_sector.hi);
        };
        auto append_event = [&](const Point& point, int kind, double fallback_angle) {
            if (squared_distance(q, point) <= point_epsilon_squared) return;
            const double geometric_angle = std::atan2(y(point) - y(q), x(point) - x(q));
            std::optional<double> angle = unwrap_angle(geometric_angle);
            if (!angle.has_value()) angle = unwrap_angle(fallback_angle);
            if (!angle.has_value()) return;
            events.push_back({*angle, kind, squared_distance(q, point), point});
        };

        for (const VisibleFragment& fragment : output.fragments) {
            append_event(fragment.a, 1, fragment.lo);
            append_event(fragment.b, 0, fragment.hi);
        }
        std::sort(events.begin(), events.end(), [](const Event& a, const Event& b) {
            if (std::abs(a.angle - b.angle) > 1e-11) return a.angle < b.angle;
            if (a.kind != b.kind) return a.kind < b.kind;
            // At an ending ray the boundary approaches the query (far-to-near).
            // At a starting ray it leaves the query (near-to-far).
            return a.kind == 0 ? a.distance2 > b.distance2
                               : a.distance2 < b.distance2;
        });

        const bool full_circle =
            visible_sector.hi - visible_sector.lo >= TWO_PI - angular_epsilon;
        std::vector<Point> ring;
        ring.reserve(events.size() + 3);
        if (!full_circle) ring.push_back(q);
        for (const Event& event : events) {
            if (ring.empty() || squared_distance(ring.back(), event.point) > 1e-16) {
                ring.push_back(event.point);
            }
        }

        // Remove a duplicate closure while computing orientation, then close once.
        if (ring.size() >= 2 && squared_distance(ring.front(), ring.back()) <= 1e-16) {
            ring.pop_back();
        }
        if (ring.size() < 3) {
            throw std::runtime_error("CDT walk produced a degenerate visibility polygon");
        }

        double twice_area = 0.0;
        for (std::size_t i = 0; i < ring.size(); ++i) {
            const Point& a = ring[i];
            const Point& b = ring[(i + 1) % ring.size()];
            twice_area += x(a) * y(b) - y(a) * x(b);
        }
        if (twice_area < 0.0) {
            if (full_circle) {
                std::reverse(ring.begin(), ring.end());
            } else {
                // Keep the boundary viewpoint as the first polygon vertex.
                std::reverse(ring.begin() + 1, ring.end());
            }
        }
        ring.push_back(ring.front());
        return ring;
    }

private:
    struct SegmentKey {
        CoordinateKey a;
        CoordinateKey b;
        bool operator==(const SegmentKey&) const = default;
    };
    struct SegmentKeyHash {
        std::size_t operator()(const SegmentKey& key) const noexcept {
            CoordinateKeyHash h;
            const std::size_t ha = h(key.a);
            const std::size_t hb = h(key.b);
            return ha ^ (hb + 0x9E3779B97F4A7C15ULL + (ha << 6U) + (ha >> 2U));
        }
    };
    struct SegmentSampleRef {
        double t = 0.0;
        std::uint32_t sample_id = 0;
    };
    struct PortalRef {
        CdtFaceHandle free_face;
        int edge_index = -1;
        Point a;
        Point b;
    };
    struct State {
        CdtFaceHandle face;
        Interval wedge;
    };
    struct VisibleFragment {
        Point a;
        Point b;
        double lo = 0.0;
        double hi = 0.0;
        std::int64_t source_segment = -1; // -1 is the non-objective outer box.
        bool query_incident_edge = false;
        Point a_ray_witness{0.0, 0.0};
        Point b_ray_witness{0.0, 0.0};
        bool a_has_ray_witness = false;
        bool b_has_ray_witness = false;
    };
    struct WalkOutput {
        std::vector<std::uint32_t> visible_samples;
        std::vector<VisibleFragment> fragments;
        std::vector<CdtVertexHandle> grazing_vertices;
        WalkStats stats;
    };
    struct FaceCoverage {
        std::uint32_t epoch = 0;
        std::uint8_t count = 0;
        std::array<Interval, 32> intervals{};
    };

    [[nodiscard]] static bool key_less(const CoordinateKey& a, const CoordinateKey& b) {
        return a.xq < b.xq || (a.xq == b.xq && a.yq < b.yq);
    }
    [[nodiscard]] static SegmentKey segment_key(const Point& a, const Point& b) {
        CoordinateKey ka = coordinate_key(x(a), y(a));
        CoordinateKey kb = coordinate_key(x(b), y(b));
        if (key_less(kb, ka)) std::swap(ka, kb);
        return {ka, kb};
    }
    [[nodiscard]] static SegmentKey segment_key(const CgalPoint& a, const CgalPoint& b) {
        return segment_key(Point(CGAL::to_double(a.x()), CGAL::to_double(a.y())),
                           Point(CGAL::to_double(b.x()), CGAL::to_double(b.y())));
    }
    [[nodiscard]] static Point to_point(const CgalPoint& p) {
        return Point(CGAL::to_double(p.x()), CGAL::to_double(p.y()));
    }
    [[nodiscard]] static Point edge_source(CdtFaceHandle face, int i) {
        return to_point(face->vertex(cdt_ccw(i))->point());
    }
    [[nodiscard]] static Point edge_target(CdtFaceHandle face, int i) {
        return to_point(face->vertex(cdt_cw(i))->point());
    }
    [[nodiscard]] static int cdt_cw(int i) { return (i + 2) % 3; }
    [[nodiscard]] static int cdt_ccw(int i) { return (i + 1) % 3; }

    void mark_component(CdtFaceHandle start, int level, std::list<CdtEdge>& border) {
        std::queue<CdtFaceHandle> queue;
        queue.push(start);
        while (!queue.empty()) {
            const CdtFaceHandle face = queue.front();
            queue.pop();
            if (face->info().nesting_level != -1) continue;
            face->info().nesting_level = level;
            for (int i = 0; i < 3; ++i) {
                const CdtFaceHandle neighbor = face->neighbor(i);
                if (neighbor->info().nesting_level != -1) continue;
                if (cdt_.is_constrained(CdtEdge(face, i))) border.emplace_back(face, i);
                else queue.push(neighbor);
            }
        }
    }

    void mark_domains() {
        for (auto face = cdt_.all_faces_begin(); face != cdt_.all_faces_end(); ++face) {
            face->info().nesting_level = -1;
        }
        std::list<CdtEdge> border;
        mark_component(cdt_.infinite_face(), 0, border);
        while (!border.empty()) {
            const CdtEdge edge = border.front();
            border.pop_front();
            const CdtFaceHandle neighbor = edge.first->neighbor(edge.second);
            if (neighbor->info().nesting_level == -1) {
                mark_component(neighbor, edge.first->info().nesting_level + 1, border);
            }
        }
        face_count_ = 0;
        for (auto face = cdt_.all_faces_begin(); face != cdt_.all_faces_end(); ++face) {
            face->info().id = static_cast<std::uint32_t>(face_count_++);
        }
    }

    void build_triangulation() {
        const double min_x = x(scene_.data_bounds.min_corner()) - config_.bbox_padding;
        const double min_y = y(scene_.data_bounds.min_corner()) - config_.bbox_padding;
        const double max_x = x(scene_.data_bounds.max_corner()) + config_.bbox_padding;
        const double max_y = y(scene_.data_bounds.max_corner()) + config_.bbox_padding;
        domain_box_ = Box(make_point(min_x, min_y), make_point(max_x, max_y));

        const std::array<Point, 4> box_points{
            make_point(min_x, min_y), make_point(max_x, min_y),
            make_point(max_x, max_y), make_point(min_x, max_y)
        };
        for (std::size_t i = 0; i < 4; ++i) {
            const Point& a = box_points[i];
            const Point& b = box_points[(i + 1) % 4];
            cdt_.insert_constraint(CgalPoint(x(a), y(a)), CgalPoint(x(b), y(b)));
            bbox_edges_.insert(segment_key(a, b));
        }
        for (std::size_t sid = 0; sid < scene_.segments.size(); ++sid) {
            const SegmentData& segment = scene_.segments[sid];
            cdt_.insert_constraint(CgalPoint(x(segment.a), y(segment.a)), CgalPoint(x(segment.b), y(segment.b)));
            if ((sid + 1) % 100000 == 0) {
                std::cout << "Inserted " << (sid + 1) << "/" << scene_.segments.size()
                          << " footprint constraints into the CDT.\n" << std::flush;
            }
        }
        if (!cdt_.is_valid()) throw std::runtime_error("CGAL constrained Delaunay triangulation is invalid");
        mark_domains();
    }

    [[nodiscard]] bool is_free(CdtFaceHandle face) const {
        return !cdt_.is_infinite(face) && face->info().in_domain();
    }

    [[nodiscard]] std::int64_t find_source_segment(const Point& a, const Point& b) const {
        const SegmentKey key = segment_key(a, b);
        const auto direct = source_segment_by_key_.find(key);
        if (direct != source_segment_by_key_.end()) return static_cast<std::int64_t>(direct->second);
        if (bbox_edges_.contains(key)) return -1;

        const double eps = std::max(1e-7, 4.0 * numerical_point_epsilon_);
        const Box query(make_point(std::min(x(a), x(b)) - eps, std::min(y(a), y(b)) - eps),
                        make_point(std::max(x(a), x(b)) + eps, std::max(y(a), y(b)) + eps));
        std::vector<SegmentValue> nearby;
        source_segment_tree_.query(bgi::intersects(query), std::back_inserter(nearby));
        for (const auto& item : nearby) {
            const SegmentData& source = scene_.segments[item.second];
            if (point_on_segment(a, source.a, source.b, eps) && point_on_segment(b, source.a, source.b, eps)) {
                return static_cast<std::int64_t>(item.second);
            }
        }
        return -2;
    }

    void index_static_edges_and_portals() {
        source_segment_by_key_.clear();
        source_segment_by_key_.reserve(scene_.segments.size() * 2);
        std::vector<SegmentValue> source_values;
        source_values.reserve(scene_.segments.size());
        for (std::uint32_t sid = 0; sid < scene_.segments.size(); ++sid) {
            const SegmentData& segment = scene_.segments[sid];
            source_segment_by_key_.emplace(segment_key(segment.a, segment.b), sid);
            source_values.emplace_back(segment_box(segment.a, segment.b), sid);
        }
        source_segment_tree_ = SegmentRTree(source_values.begin(), source_values.end());

        cdt_vertex_by_key_.clear();
        cdt_vertex_by_key_.reserve(cdt_.number_of_vertices() * 2);
        for (auto vertex = cdt_.finite_vertices_begin();
             vertex != cdt_.finite_vertices_end(); ++vertex) {
            const CgalPoint& point = vertex->point();
            cdt_vertex_by_key_.emplace(
                coordinate_key(CGAL::to_double(point.x()), CGAL::to_double(point.y())),
                CdtVertexHandle(vertex));
        }

        segment_portals_.assign(scene_.segments.size(), {});
        constrained_edge_source_.clear();
        constrained_edge_source_.reserve(cdt_.number_of_faces());
        for (auto edge = cdt_.finite_edges_begin(); edge != cdt_.finite_edges_end(); ++edge) {
            if (!cdt_.is_constrained(*edge)) continue;
            CdtFaceHandle face = edge->first;
            const int i = edge->second;
            const Point a = edge_source(face, i);
            const Point b = edge_target(face, i);
            const std::int64_t sid = find_source_segment(a, b);
            constrained_edge_source_[segment_key(a, b)] = sid;
            if (sid < 0) continue;

            CdtFaceHandle free_face;
            int free_i = -1;
            if (is_free(face)) {
                free_face = face;
                free_i = i;
            } else {
                const CdtFaceHandle neighbor = face->neighbor(i);
                if (is_free(neighbor)) {
                    free_face = neighbor;
                    free_i = neighbor->index(face);
                }
            }
            if (free_i >= 0) {
                segment_portals_[static_cast<std::size_t>(sid)].push_back({free_face, free_i, a, b});
            }
        }
    }

    void index_samples() {
        segment_samples_.assign(scene_.segments.size(), {});
        for (std::uint32_t sample_id = 0; sample_id < scene_.samples.size(); ++sample_id) {
            const BoundaryPoint& sample = scene_.samples[sample_id];
            // Candidate locations have zero weight and are not coverage atoms.
            if (!(sample.weight > 0.0)) continue;
            for (const std::uint32_t sid : sample.incident_segments) {
                if (sid == std::numeric_limits<std::uint32_t>::max()) continue;
                const SegmentData& segment = scene_.segments[sid];
                const Point d = subtract(segment.b, segment.a);
                const double denom = dot(d, d);
                const double t = denom > 0.0
                    ? std::clamp(dot(subtract(sample.point, segment.a), d) / denom, 0.0, 1.0)
                    : 0.0;
                segment_samples_[sid].push_back({t, sample_id});
            }
        }
        for (auto& refs : segment_samples_) {
            std::sort(refs.begin(), refs.end(), [](const SegmentSampleRef& a, const SegmentSampleRef& b) {
                if (a.t != b.t) return a.t < b.t;
                return a.sample_id < b.sample_id;
            });
            refs.erase(std::unique(refs.begin(), refs.end(), [](const SegmentSampleRef& a, const SegmentSampleRef& b) {
                return a.sample_id == b.sample_id;
            }), refs.end());
        }
    }

    [[nodiscard]] static Interval witnessed_interval(
        double lo,
        double hi,
        const Point& lo_witness,
        const Point& hi_witness)
    {
        Interval interval;
        interval.lo = lo;
        interval.hi = hi;
        interval.lo_ray_witness = lo_witness;
        interval.hi_ray_witness = hi_witness;
        interval.lo_has_witness = true;
        interval.hi_has_witness = true;
        return interval;
    }

    [[nodiscard]] static Point seam_ray_witness(const Point& query) {
        return make_point(x(query) + 1.0, y(query));
    }

    [[nodiscard]] std::vector<Interval> face_angular_sectors(
        CdtFaceHandle face,
        const Point& query) const
    {
        constexpr double angular_epsilon = 1e-13;
        const double point_epsilon = numerical_point_epsilon_;
        const double point_epsilon_squared = point_epsilon * point_epsilon;

        struct AngularRay {
            double angle = 0.0;
            Point witness{0.0, 0.0};
        };
        std::vector<AngularRay> rays;
        rays.reserve(3);
        for (int v = 0; v < 3; ++v) {
            const Point point = to_point(face->vertex(v)->point());
            if (squared_distance(query, point) <= point_epsilon_squared) continue;
            rays.push_back({
                normalize_angle(std::atan2(y(point) - y(query), x(point) - x(query))),
                point});
        }
        std::sort(rays.begin(), rays.end(), [](const AngularRay& a, const AngularRay& b) {
            return a.angle < b.angle;
        });
        rays.erase(std::unique(rays.begin(), rays.end(), [](const AngularRay& a, const AngularRay& b) {
            return std::abs(a.angle - b.angle) <= angular_epsilon;
        }), rays.end());
        if (rays.size() < 2) return {};

        // The query lies on this triangle's boundary. The triangle therefore
        // occupies the complement of the largest angular gap between its vertex
        // rays. This works both at a triangle vertex and in the interior of an edge.
        std::size_t largest_gap_after = 0;
        double largest_gap = -1.0;
        for (std::size_t i = 0; i < rays.size(); ++i) {
            const double next = i + 1 < rays.size()
                ? rays[i + 1].angle
                : rays.front().angle + TWO_PI;
            const double gap = next - rays[i].angle;
            if (gap > largest_gap) {
                largest_gap = gap;
                largest_gap_after = i;
            }
        }

        const AngularRay& lo_ray = rays[(largest_gap_after + 1) % rays.size()];
        const AngularRay& hi_ray = rays[largest_gap_after];
        double lo = lo_ray.angle;
        double hi = hi_ray.angle;
        if (hi < lo) hi += TWO_PI;
        const double width = hi - lo;
        if (width <= angular_epsilon) return {};

        // A width above pi means the point was numerically classified inside the
        // face rather than on its boundary. Full-circle seeding is the safe fallback.
        if (width > PI + 1e-10) {
            const Point seam = seam_ray_witness(query);
            return {witnessed_interval(0.0, TWO_PI, seam, seam)};
        }

        std::vector<Interval> sectors;
        if (hi <= TWO_PI + angular_epsilon) {
            sectors.push_back(witnessed_interval(
                lo, std::min(hi, TWO_PI), lo_ray.witness, hi_ray.witness));
        } else {
            const Point seam = seam_ray_witness(query);
            sectors.push_back(witnessed_interval(lo, TWO_PI, lo_ray.witness, seam));
            const double wrapped_hi = hi - TWO_PI;
            if (wrapped_hi > angular_epsilon) {
                sectors.push_back(witnessed_interval(0.0, wrapped_hi, seam, hi_ray.witness));
            }
        }
        return sectors;
    }

    void index_query_sites() {
        query_sites_.clear();
        query_sites_.reserve(scene_.candidates.size());
        const double eps = std::max(1e-8, 4.0 * numerical_point_epsilon_);
        const double eps_squared = eps * eps;

        for (std::size_t ci = 0; ci < scene_.candidates.size(); ++ci) {
            const BoundaryPoint& sample = scene_.samples[scene_.candidates[ci]];
            const CgalPoint exact_query(x(sample.point), y(sample.point));
            QuerySite site;
            site.query_point = exact_query;

            Cdt::Locate_type locate_type;
            int locate_index = -1;
            const CdtFaceHandle located_face =
                cdt_.locate(exact_query, locate_type, locate_index);

            // A genuinely interior normal-offset candidate gets a full angular
            // seed. If the prescribed edge-normal displacement lands exactly on
            // another source edge (common at right-angle vertices), retain that
            // exact displaced point and handle it as a boundary query below.
            if (sample.offset_from_boundary && locate_type == Cdt::FACE) {
                if (!is_free(located_face)) {
                    throw std::runtime_error(
                        "Normal-offset candidate " + std::to_string(ci) +
                        " landed in an obstacle CDT face");
                }
                site.interior_query = true;
                const Point seam = seam_ray_witness(sample.point);
                site.seeds.push_back({
                    located_face,
                    witnessed_interval(0.0, TWO_PI, seam, seam)});
                query_sites_.push_back(std::move(site));
                if ((ci + 1) % 100000 == 0) {
                    std::cout << "Indexed " << (ci + 1) << "/" << scene_.candidates.size()
                              << " interior normal-offset query sites.\n";
                }
                continue;
            }

            // The incident fan is tiny. Deduplicate it locally instead of
            // allocating and clearing a face_count-sized bitmap per candidate.
            std::vector<CdtFaceHandle> incident_free_faces;
            incident_free_faces.reserve(8);
            auto append_face = [&](CdtFaceHandle face) {
                if (!is_free(face)) return;
                const std::uint32_t id = face->info().id;
                for (const CdtFaceHandle prior : incident_free_faces) {
                    if (prior->info().id == id) return;
                }
                incident_free_faces.push_back(face);
            };

            if (locate_type == Cdt::VERTEX && locate_index >= 0) {
                const CdtVertexHandle vertex = located_face->vertex(locate_index);
                auto face = cdt_.incident_faces(vertex);
                const auto done = face;
                if (face != 0) {
                    do {
                        append_face(face);
                        ++face;
                    } while (face != done);
                }
            } else if (locate_type == Cdt::EDGE && locate_index >= 0) {
                append_face(located_face);
                append_face(located_face->neighbor(locate_index));
            }

            // Numerical fallback for boundary points that CGAL classified as a
            // face query. This lookup never merges candidates; it only identifies
            // the CDT fan incident to the already-fixed query coordinate.
            if (incident_free_faces.empty()) {
                const auto vertex_found = cdt_vertex_by_key_.find(
                    coordinate_key(x(sample.point), y(sample.point)));
                if (vertex_found != cdt_vertex_by_key_.end()) {
                    const Point indexed_point = to_point(vertex_found->second->point());
                    if (squared_distance(indexed_point, sample.point) <= eps_squared) {
                        auto face = cdt_.incident_faces(vertex_found->second);
                        const auto done = face;
                        if (face != 0) {
                            do {
                                append_face(face);
                                ++face;
                            } while (face != done);
                        }
                    }
                }
            }

            auto append_portals_for_segment = [&](std::uint32_t sid) {
                for (const PortalRef& portal : segment_portals_.at(sid)) {
                    if (point_on_segment(sample.point, portal.a, portal.b, eps)) {
                        append_face(portal.free_face);
                    }
                }
            };
            if (incident_free_faces.empty()) {
                for (const std::uint32_t sid : sample.incident_segments) {
                    if (sid == std::numeric_limits<std::uint32_t>::max()) continue;
                    append_portals_for_segment(sid);
                }
            }
            if (incident_free_faces.empty()) {
                const Box query(
                    make_point(x(sample.point) - eps, y(sample.point) - eps),
                    make_point(x(sample.point) + eps, y(sample.point) + eps));
                std::vector<SegmentValue> nearby;
                source_segment_tree_.query(bgi::intersects(query), std::back_inserter(nearby));
                for (const SegmentValue& item : nearby) {
                    const SegmentData& source = scene_.segments[item.second];
                    if (point_on_segment(sample.point, source.a, source.b, eps)) {
                        append_portals_for_segment(item.second);
                    }
                }
            }

            for (const CdtFaceHandle face : incident_free_faces) {
                for (const Interval sector : face_angular_sectors(face, sample.point)) {
                    if (sector.hi - sector.lo > 1e-13) {
                        site.seeds.push_back({face, sector});
                    }
                }
            }
            if (site.seeds.empty()) {
                throw std::runtime_error(
                    "Could not map candidate " + std::to_string(ci) +
                    " to an exact free-space CDT sector");
            }

            std::sort(site.seeds.begin(), site.seeds.end(), [](const QuerySeed& a, const QuerySeed& b) {
                if (a.start_face->info().id != b.start_face->info().id) {
                    return a.start_face->info().id < b.start_face->info().id;
                }
                if (a.wedge.lo != b.wedge.lo) return a.wedge.lo < b.wedge.lo;
                return a.wedge.hi < b.wedge.hi;
            });
            query_sites_.push_back(std::move(site));

            if ((ci + 1) % 100000 == 0) {
                std::cout << "Indexed " << (ci + 1) << "/" << scene_.candidates.size()
                          << " exact boundary query sites.\n";
            }
        }
    }

    [[nodiscard]] static std::array<Interval, 2> segment_angle_intervals(
        const Point& q, const Point& a, const Point& b, int& count)
    {
        double aa = normalize_angle(std::atan2(y(a) - y(q), x(a) - x(q)));
        const double bb0 = normalize_angle(std::atan2(y(b) - y(q), x(b) - x(q)));
        double diff = std::remainder(bb0 - aa, TWO_PI);
        if (std::abs(std::abs(diff) - PI) < 1e-12) diff = bb0 >= aa ? PI : -PI;
        const double bb = aa + diff;

        double lo = aa;
        double hi = bb;
        Point lo_witness = a;
        Point hi_witness = b;
        if (hi < lo) {
            std::swap(lo, hi);
            std::swap(lo_witness, hi_witness);
        }
        while (lo < 0.0) { lo += TWO_PI; hi += TWO_PI; }
        while (lo >= TWO_PI) { lo -= TWO_PI; hi -= TWO_PI; }

        std::array<Interval, 2> result{};
        if (hi <= TWO_PI + 1e-14) {
            result[0] = witnessed_interval(
                lo, std::min(hi, TWO_PI), lo_witness, hi_witness);
            count = 1;
        } else {
            const Point seam = seam_ray_witness(q);
            result[0] = witnessed_interval(lo, TWO_PI, lo_witness, seam);
            result[1] = witnessed_interval(0.0, hi - TWO_PI, seam, hi_witness);
            count = 2;
        }
        return result;
    }

    [[nodiscard]] static Point ray_segment_intersection(
        const Point& q, double angle, const Point& a, const Point& b)
    {
        const Point d(std::cos(angle), std::sin(angle));
        const Point v = subtract(b, a);
        const double denom = cross(v, d);
        if (std::abs(denom) <= 1e-15) {
            const double da = std::abs(std::remainder(std::atan2(y(a)-y(q), x(a)-x(q)) - angle, TWO_PI));
            const double db = std::abs(std::remainder(std::atan2(y(b)-y(q), x(b)-x(q)) - angle, TWO_PI));
            return da <= db ? a : b;
        }
        const double t = std::clamp(cross(subtract(q, a), d) / denom, 0.0, 1.0);
        return make_point(x(a) + t * x(v), y(a) + t * y(v));
    }

    [[nodiscard]] static std::vector<Interval> uncovered_and_record(
        FaceCoverage& coverage,
        Interval input,
        std::uint32_t epoch)
    {
        constexpr double eps = 1e-13;
        if (coverage.epoch != epoch) {
            coverage.epoch = epoch;
            coverage.count = 0;
        }
        std::vector<Interval> uncovered{input};
        for (std::uint8_t i = 0; i < coverage.count && !uncovered.empty(); ++i) {
            const Interval used = coverage.intervals[i];
            std::vector<Interval> next;
            next.reserve(uncovered.size() + 1);
            for (const Interval part : uncovered) {
                if (used.hi <= part.lo + eps || used.lo >= part.hi - eps) {
                    next.push_back(part);
                    continue;
                }
                if (used.lo > part.lo + eps) {
                    Interval left = part;
                    left.hi = std::min(part.hi, used.lo);
                    if (used.lo < part.hi) {
                        left.hi_ray_witness = used.lo_ray_witness;
                        left.hi_has_witness = used.lo_has_witness;
                    }
                    next.push_back(left);
                }
                if (used.hi < part.hi - eps) {
                    Interval right = part;
                    right.lo = std::max(part.lo, used.hi);
                    if (used.hi > part.lo) {
                        right.lo_ray_witness = used.hi_ray_witness;
                        right.lo_has_witness = used.hi_has_witness;
                    }
                    next.push_back(right);
                }
            }
            uncovered.swap(next);
        }
        if (uncovered.empty()) return uncovered;

        std::vector<Interval> all;
        all.reserve(static_cast<std::size_t>(coverage.count) + 1);
        for (std::uint8_t i = 0; i < coverage.count; ++i) all.push_back(coverage.intervals[i]);
        all.push_back(input);
        std::sort(all.begin(), all.end(), [](const Interval& a, const Interval& b) {
            return a.lo < b.lo;
        });
        std::array<Interval, 8> merged{};
        std::size_t merged_count = 0;
        for (const Interval& current : all) {
            if (merged_count == 0 || current.lo > merged[merged_count - 1].hi + eps) {
                if (merged_count < merged.size()) {
                    merged[merged_count++] = current;
                } else if (current.hi > merged[merged_count - 1].hi) {
                    merged[merged_count - 1].hi = current.hi;
                    merged[merged_count - 1].hi_ray_witness = current.hi_ray_witness;
                    merged[merged_count - 1].hi_has_witness = current.hi_has_witness;
                }
            } else if (current.hi > merged[merged_count - 1].hi) {
                merged[merged_count - 1].hi = current.hi;
                merged[merged_count - 1].hi_ray_witness = current.hi_ray_witness;
                merged[merged_count - 1].hi_has_witness = current.hi_has_witness;
            }
        }
        coverage.count = static_cast<std::uint8_t>(merged_count);
        for (std::size_t i = 0; i < merged_count; ++i) coverage.intervals[i] = merged[i];
        return uncovered;
    }

    [[nodiscard]] std::int64_t constrained_source(CdtFaceHandle face, int i) const {
        const SegmentKey key = segment_key(edge_source(face, i), edge_target(face, i));
        const auto found = constrained_edge_source_.find(key);
        return found == constrained_edge_source_.end() ? -2 : found->second;
    }

    void append_fragment_samples(std::int64_t source_segment, const Point& a, const Point& b,
                                 std::vector<std::uint32_t>& out) const
    {
        if (source_segment < 0) return;
        const std::size_t sid = static_cast<std::size_t>(source_segment);
        const SegmentData& source = scene_.segments[sid];
        const Point d = subtract(source.b, source.a);
        const double denom = dot(d, d);
        if (denom <= 1e-20) return;
        double t0 = dot(subtract(a, source.a), d) / denom;
        double t1 = dot(subtract(b, source.a), d) / denom;
        if (t0 > t1) std::swap(t0, t1);
        const double eps_t = std::max(1e-10, numerical_point_epsilon_ / std::sqrt(denom));
        t0 -= eps_t;
        t1 += eps_t;
        const auto& refs = segment_samples_[sid];
        auto it = std::lower_bound(refs.begin(), refs.end(), t0, [](const SegmentSampleRef& ref, double t) { return ref.t < t; });
        for (; it != refs.end() && it->t <= t1; ++it) out.push_back(it->sample_id);
    }

    void append_incident_boundary_visibility(
        const BoundaryPoint& sample,
        const Point& query,
        bool collect_fragments,
        WalkOutput& output) const
    {
        std::vector<std::uint32_t> seen;
        seen.reserve(4);
        const double point_epsilon = numerical_point_epsilon_;
        const double point_epsilon_squared = point_epsilon * point_epsilon;
        const double containment_epsilon =
            std::max(1e-8, 4.0 * numerical_point_epsilon_);

        auto append_segment = [&](std::uint32_t sid) {
            if (std::find(seen.begin(), seen.end(), sid) != seen.end()) return;
            const SegmentData& segment = scene_.segments.at(sid);
            if (!point_on_segment(query, segment.a, segment.b, containment_epsilon)) return;
            seen.push_back(sid);

            auto append_half = [&](const Point& endpoint) {
                if (squared_distance(query, endpoint) <= point_epsilon_squared) return;
                append_fragment_samples(sid, query, endpoint, output.visible_samples);
                if (collect_fragments) {
                    const double angle = normalize_angle(
                        std::atan2(y(endpoint) - y(query), x(endpoint) - x(query)));
                    output.fragments.push_back({
                        query,
                        endpoint,
                        angle,
                        angle,
                        static_cast<std::int64_t>(sid),
                        true,
                        Point(0.0, 0.0),
                        Point(0.0, 0.0),
                        false,
                        false});
                }
            };
            append_half(segment.a);
            append_half(segment.b);
        };

        for (const std::uint32_t sid : sample.incident_segments) {
            if (sid != std::numeric_limits<std::uint32_t>::max()) append_segment(sid);
        }

        // A prescribed normal displacement can land exactly on the other edge at
        // a right-angle vertex. Discover that actual containing edge without
        // changing or snapping the candidate coordinate.
        const Box query_box(
            make_point(x(query) - containment_epsilon, y(query) - containment_epsilon),
            make_point(x(query) + containment_epsilon, y(query) + containment_epsilon));
        std::vector<SegmentValue> nearby;
        source_segment_tree_.query(bgi::intersects(query_box), std::back_inserter(nearby));
        for (const SegmentValue& item : nearby) append_segment(item.second);
    }

    [[nodiscard]] CdtVertexHandle cdt_vertex_at(
        const Point& point,
        double epsilon_squared) const
    {
        const auto found = cdt_vertex_by_key_.find(coordinate_key(x(point), y(point)));
        if (found == cdt_vertex_by_key_.end()) return CdtVertexHandle();
        if (squared_distance(to_point(found->second->point()), point) > epsilon_squared) {
            return CdtVertexHandle();
        }
        return found->second;
    }

    void append_local_collinear_chain(
        CdtVertexHandle start_vertex,
        const Point& query,
        const Point& unit,
        WalkOutput& output) const
    {
        if (start_vertex == CdtVertexHandle()) return;

        const double point_epsilon =
            numerical_point_epsilon_;
        const double line_epsilon =
            std::max(1e-11, 2.0 * numerical_point_epsilon_);

        std::vector<CdtVertexHandle> stack;
        stack.reserve(8);
        stack.push_back(start_vertex);
        std::vector<CoordinateKey> visited_vertices;
        visited_vertices.reserve(8);

        while (!stack.empty()) {
            const CdtVertexHandle vertex = stack.back();
            stack.pop_back();
            const Point current = to_point(vertex->point());
            const CoordinateKey current_key = coordinate_key(x(current), y(current));
            if (std::find(visited_vertices.begin(), visited_vertices.end(), current_key) !=
                visited_vertices.end()) {
                continue;
            }
            visited_vertices.push_back(current_key);

            const double current_t = dot(subtract(current, query), unit);
            std::vector<SegmentKey> seen_edges;
            seen_edges.reserve(8);

            auto face = cdt_.incident_faces(vertex);
            const auto done = face;
            if (face == 0) continue;
            do {
                for (int i = 0; i < 3; ++i) {
                    const CdtEdge edge(face, i);
                    if (!cdt_.is_constrained(edge)) continue;

                    const CdtVertexHandle va = face->vertex(cdt_ccw(i));
                    const CdtVertexHandle vb = face->vertex(cdt_cw(i));
                    CdtVertexHandle other;
                    if (va == vertex) other = vb;
                    else if (vb == vertex) other = va;
                    else continue;

                    const Point other_point = to_point(other->point());
                    const SegmentKey key = segment_key(current, other_point);
                    if (std::find(seen_edges.begin(), seen_edges.end(), key) !=
                        seen_edges.end()) {
                        continue;
                    }
                    seen_edges.push_back(key);

                    const Point delta = subtract(other_point, query);
                    const double forward_t = dot(delta, unit);
                    const double delta_length = std::hypot(x(delta), y(delta));
                    if (forward_t <= current_t + point_epsilon ||
                        std::abs(cross(unit, delta)) >
                            line_epsilon * std::max(1.0, delta_length)) {
                        continue;
                    }

                    const std::int64_t sid = constrained_source(face, i);
                    if (sid < 0) continue;
                    append_fragment_samples(sid, current, other_point, output.visible_samples);
                    stack.push_back(other);
                }
                ++face;
            } while (face != done);
        }
    }

    [[nodiscard]] WalkOutput walk(
        std::size_t candidate_index,
        bool collect_fragments) const
    {
        if (candidate_index >= query_sites_.size()) throw std::out_of_range("candidate index");
        const QuerySite& site = query_sites_[candidate_index];
        const Point q = to_point(site.query_point);
        const BoundaryPoint& query_sample =
            scene_.samples.at(scene_.candidates.at(candidate_index));

        thread_local std::vector<FaceCoverage> coverage;
        thread_local std::uint32_t epoch = 1;
        if (coverage.size() < face_count_) coverage.resize(face_count_);
        if (++epoch == 0) {
            for (FaceCoverage& c : coverage) c.epoch = 0;
            epoch = 1;
        }

        WalkOutput output;
        output.visible_samples.reserve(128);
        output.grazing_vertices.reserve(16);
        if (collect_fragments) output.fragments.reserve(64);
        if (!site.interior_query) {
            append_incident_boundary_visibility(
                query_sample, q, collect_fragments, output);
        }

        std::vector<State> stack;
        stack.reserve(128 + site.seeds.size());
        for (const QuerySeed& seed : site.seeds) {
            stack.push_back({seed.start_face, seed.wedge});
        }

        constexpr double angular_eps = 1e-12;
        const double query_edge_epsilon =
            numerical_point_epsilon_;
        while (!stack.empty()) {
            const State state = stack.back();
            stack.pop_back();
            if (!is_free(state.face)) continue;
            FaceCoverage& face_coverage = coverage[state.face->info().id];
            const std::vector<Interval> new_parts = uncovered_and_record(face_coverage, state.wedge, epoch);
            for (const Interval part : new_parts) {
                if (part.hi - part.lo <= angular_eps) continue;
                ++output.stats.faces;
                for (int i = 0; i < 3; ++i) {
                    const Point ea = edge_source(state.face, i);
                    const Point eb = edge_target(state.face, i);
                    // Edges containing the exact viewpoint bound an initial sector.
                    // Their angular width is zero and they must not be treated as
                    // ordinary blockers or portals. Incident source-boundary samples
                    // were added explicitly above.
                    if (point_on_segment(q, ea, eb, query_edge_epsilon)) continue;
                    int interval_count = 0;
                    const auto edge_intervals = segment_angle_intervals(q, ea, eb, interval_count);
                    for (int pi = 0; pi < interval_count; ++pi) {
                        const Interval edge_interval = edge_intervals[pi];
                        Interval overlap;
                        if (edge_interval.lo >= part.lo) {
                            overlap.lo = edge_interval.lo;
                            overlap.lo_ray_witness = edge_interval.lo_ray_witness;
                            overlap.lo_has_witness = edge_interval.lo_has_witness;
                        } else {
                            overlap.lo = part.lo;
                            overlap.lo_ray_witness = part.lo_ray_witness;
                            overlap.lo_has_witness = part.lo_has_witness;
                        }
                        if (edge_interval.hi <= part.hi) {
                            overlap.hi = edge_interval.hi;
                            overlap.hi_ray_witness = edge_interval.hi_ray_witness;
                            overlap.hi_has_witness = edge_interval.hi_has_witness;
                        } else {
                            overlap.hi = part.hi;
                            overlap.hi_ray_witness = part.hi_ray_witness;
                            overlap.hi_has_witness = part.hi_has_witness;
                        }
                        if (overlap.hi - overlap.lo <= angular_eps) continue;
                        Point ca = ray_segment_intersection(q, overlap.lo, ea, eb);
                        Point cb = ray_segment_intersection(q, overlap.hi, ea, eb);
                        const CdtFaceHandle neighbor = state.face->neighbor(i);
                        const bool blocked = cdt_.is_constrained(CdtEdge(state.face, i)) || !is_free(neighbor);
                        if (blocked) {
                            ++output.stats.blockers;
                            const std::int64_t sid = cdt_.is_constrained(CdtEdge(state.face, i))
                                ? constrained_source(state.face, i) : -2;
                            append_fragment_samples(sid, ca, cb, output.visible_samples);
                            if (sid >= 0) {
                                const double vertex_epsilon = numerical_point_epsilon_;
                                const double vertex_epsilon_squared =
                                    vertex_epsilon * vertex_epsilon;
                                for (const Point& endpoint : {ca, cb}) {
                                    const CdtVertexHandle vertex =
                                        cdt_vertex_at(endpoint, vertex_epsilon_squared);
                                    if (vertex != CdtVertexHandle() &&
                                        squared_distance(endpoint, q) > vertex_epsilon_squared) {
                                        output.grazing_vertices.push_back(vertex);
                                    }
                                }
                            }
                            if (collect_fragments) {
                                output.fragments.push_back({
                                    ca,
                                    cb,
                                    overlap.lo,
                                    overlap.hi,
                                    sid,
                                    false,
                                    overlap.lo_ray_witness,
                                    overlap.hi_ray_witness,
                                    overlap.lo_has_witness,
                                    overlap.hi_has_witness});
                            }
                        } else {
                            ++output.stats.portals;
                            stack.push_back({neighbor, overlap});
                        }
                    }
                }
            }
        }

        // Preserve directly connected collinear boundary continuations using
        // only the local constrained-edge fan at each visible CDT vertex. We do
        // not launch global R-tree rays or full supporting-line walks.
        std::vector<Point> processed_directions;
        if (!site.interior_query) {
            processed_directions.reserve(output.grazing_vertices.size());
        }
        const double grazing_epsilon =
            numerical_point_epsilon_;
        const double grazing_epsilon_squared = grazing_epsilon * grazing_epsilon;
        if (!site.interior_query) for (const CdtVertexHandle vertex : output.grazing_vertices) {
            const Point vertex_point = to_point(vertex->point());
            const Point delta = subtract(vertex_point, q);
            const double length = std::hypot(x(delta), y(delta));
            if (length * length <= grazing_epsilon_squared) continue;
            const Point unit = make_point(x(delta) / length, y(delta) / length);
            bool duplicate = false;
            for (const Point& prior : processed_directions) {
                if (std::abs(cross(unit, prior)) <= 1e-12 &&
                    dot(unit, prior) > 1.0 - 1e-12) {
                    duplicate = true;
                    break;
                }
            }
            if (duplicate) continue;
            processed_directions.push_back(unit);
            append_local_collinear_chain(vertex, q, unit, output);
        }

        std::sort(output.visible_samples.begin(), output.visible_samples.end());
        output.visible_samples.erase(std::unique(output.visible_samples.begin(), output.visible_samples.end()), output.visible_samples.end());
        return output;
    }

    const Scene& scene_;
    VisibilityConfig config_;
    Box domain_box_;
    Cdt cdt_;
    std::size_t face_count_ = 0;
    std::unordered_set<SegmentKey, SegmentKeyHash> bbox_edges_;
    std::unordered_map<SegmentKey, std::uint32_t, SegmentKeyHash> source_segment_by_key_;
    std::unordered_map<SegmentKey, std::int64_t, SegmentKeyHash> constrained_edge_source_;
    std::unordered_map<CoordinateKey, CdtVertexHandle, CoordinateKeyHash> cdt_vertex_by_key_;
    SegmentRTree source_segment_tree_;
    std::vector<std::vector<SegmentSampleRef>> segment_samples_;
    std::vector<std::vector<PortalRef>> segment_portals_;
    std::vector<QuerySite> query_sites_;
    double numerical_point_epsilon_ = 1e-10;
};


void build_visibility_polygon_vertex_candidates(
    Scene& scene,
    const VisibilityEngine& engine,
    double boundary_epsilon,
    double max_vertex_distance,
    std::size_t progress_every)
{
    const std::size_t source_count = scene.candidates.size();
    std::vector<BoundaryPoint> original_vertices;
    original_vertices.reserve(source_count);
    for (const std::uint32_t sample_id : scene.candidates) {
        original_vertices.push_back(scene.samples.at(sample_id));
    }

    constexpr std::size_t block_size = 256;
    std::vector<std::vector<VisibilityEngine::CandidateVertex>> generated(source_count);
    std::vector<VisibilityEngine::WalkStats> stats(source_count);
    std::uint64_t total_faces = 0;
    std::uint64_t total_vertices = 0;
    const auto start = std::chrono::steady_clock::now();
    std::size_t next_report = progress_every > 0 ? progress_every : source_count + 1;

    for (std::size_t base = 0; base < source_count; base += block_size) {
        const std::size_t count = std::min(block_size, source_count - base);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(dynamic, 8)
#endif
        for (std::int64_t local_signed = 0;
             local_signed < static_cast<std::int64_t>(count);
             ++local_signed) {
            const std::size_t local = static_cast<std::size_t>(local_signed);
            generated[base + local] = engine.compute_visibility_candidates(
                base + local, max_vertex_distance, &stats[base + local]);
        }

        for (std::size_t local = 0; local < count; ++local) {
            total_faces += stats[base + local].faces;
            total_vertices += generated[base + local].size();
        }
        const std::size_t done = base + count;
        if (progress_every > 0 && (done >= next_report || done == source_count)) {
            while (next_report <= done) next_report += progress_every;
            const double seconds = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - start).count();
            std::cerr << "Vertex visibility candidates: " << done << "/" << source_count
                      << " visibility polygons, " << std::fixed << std::setprecision(1)
                      << (seconds > 0.0 ? static_cast<double>(done) / seconds : 0.0)
                      << "/s, avg "
                      << (done > 0 ? static_cast<double>(total_faces) / static_cast<double>(done) : 0.0)
                      << " faces and "
                      << (done > 0 ? static_cast<double>(total_vertices) / static_cast<double>(done) : 0.0)
                      << " candidate vertices/polygon.\n";
        }
    }

    std::size_t nongeneral_incident_counts = 0;
    for (const auto& vertices : generated) {
        const std::size_t incident_count = static_cast<std::size_t>(std::count_if(
            vertices.begin(), vertices.end(),
            [](const VisibilityEngine::CandidateVertex& vertex) {
                return vertex.slide_on_landing_edge;
            }));
        if (incident_count != 2) ++nongeneral_incident_counts;
    }

    clear_boundary_discretization(scene);

    struct PendingCandidate {
        BoundaryPoint candidate;
        Point ray_origin{0.0, 0.0};
        Point ray_witness{0.0, 0.0};
        bool has_ray_witness = false;
        bool keep = true;
    };

    constexpr std::size_t no_pending = std::numeric_limits<std::size_t>::max();
    struct IdentityNode {
        std::size_t pending_index = std::numeric_limits<std::size_t>::max();
        std::uint32_t existing_sample_id =
            std::numeric_limits<std::uint32_t>::max();
        ParameterInterval interval;
        std::optional<VerifierFT> exact_parameter;
    };

    // Groups exist only for edges that actually receive candidate occurrences.
    // The low key bit is the slide class, which intentionally keeps an exact
    // boundary point separate from its deliberately boundary-slid candidate.
    std::unordered_map<std::uint64_t, std::vector<IdentityNode>> identity_groups;
    identity_groups.reserve(scene.segments.size());
    auto group_key = [](std::uint32_t sid, bool slide) {
        return (static_cast<std::uint64_t>(sid) << 1U) |
               static_cast<std::uint64_t>(slide);
    };

    // Original polygon vertices are exact endpoints of both incident edges.
    // Register 0/1 directly, avoiding exact-kernel work even here.
    for (BoundaryPoint candidate : original_vertices) {
        const auto sample_id = static_cast<std::uint32_t>(scene.samples.size());
        append_candidate_sample(scene, candidate);
        for (const std::uint32_t sid : candidate.incident_segments) {
            if (sid == std::numeric_limits<std::uint32_t>::max()) continue;
            const SegmentData& segment = scene.segments.at(sid);
            VerifierFT exact_parameter;
            ParameterInterval interval;
            if (exact_point_equal(candidate.point, segment.a)) {
                exact_parameter = VerifierFT(0);
                interval = {0.0L, 0.0L};
            } else if (exact_point_equal(candidate.point, segment.b)) {
                exact_parameter = VerifierFT(1);
                interval = {1.0L, 1.0L};
            } else {
                // Defensive fallback for malformed rings; ordinary input never
                // reaches this branch because original vertices are endpoints.
                exact_parameter =
                    exact_segment_parameter_from_point(segment, candidate.point);
                interval = segment_parameter_interval_from_point(segment, candidate.point);
            }
            identity_groups[group_key(sid, false)].push_back({
                no_pending,
                sample_id,
                interval,
                std::move(exact_parameter)});
        }
    }

    const std::uint32_t no_segment = std::numeric_limits<std::uint32_t>::max();
    std::vector<std::uint32_t> previous_ring_segment(scene.segments.size(), no_segment);
    std::vector<std::uint32_t> next_ring_segment(scene.segments.size(), no_segment);
    for (const PolygonData& polygon : scene.polygons) {
        for (const RingData& ring : polygon.rings) {
            const std::size_t n = ring.segment_ids.size();
            if (n == 0) continue;
            for (std::size_t i = 0; i < n; ++i) {
                const std::uint32_t sid = ring.segment_ids[i];
                previous_ring_segment.at(sid) = ring.segment_ids[(i + n - 1) % n];
                next_ring_segment.at(sid) = ring.segment_ids[(i + 1) % n];
            }
        }
    }

    std::vector<PendingCandidate> pending;
    pending.reserve(static_cast<std::size_t>(total_vertices));
    std::size_t slid = 0;
    std::size_t clamped_slides = 0;
    std::size_t near_orthogonal_slides = 0;
    for (const auto& polygon_vertices : generated) {
        for (const VisibilityEngine::CandidateVertex& vertex : polygon_vertices) {
            const std::uint32_t source_sid = vertex.generating_segment;
            const SegmentData& source_segment = scene.segments.at(source_sid);

            std::uint32_t identity_sid = source_sid;
            const SegmentData* identity_segment = &source_segment;
            double parameter = 0.0;
            ParameterInterval interval;
            std::optional<VerifierFT> prescribed_exact_parameter;

            BoundaryPoint candidate;
            candidate.boundary_anchor = vertex.boundary_point;
            candidate.point = candidate.boundary_anchor;

            if (vertex.slide_on_landing_edge) {
                // The collinearity vertex is the far endpoint of one of the two
                // edges incident to the source viewpoint.  Keep the candidate on
                // the boundary by continuing from that endpoint along the other
                // ring edge.  The normal of the generating edge chooses the
                // tangential direction; it is not used as an off-boundary
                // displacement vector.
                bool landed_at_source_a = exact_point_equal(
                    vertex.boundary_point, source_segment.a);
                const bool landed_at_source_b = exact_point_equal(
                    vertex.boundary_point, source_segment.b);
                if (!landed_at_source_a && !landed_at_source_b) {
                    const double da = squared_distance(
                        vertex.boundary_point, source_segment.a);
                    const double db = squared_distance(
                        vertex.boundary_point, source_segment.b);
                    landed_at_source_a = da <= db;
                }

                identity_sid = landed_at_source_a
                    ? previous_ring_segment.at(source_sid)
                    : next_ring_segment.at(source_sid);
                if (identity_sid == no_segment || identity_sid == source_sid) {
                    throw std::runtime_error(
                        "Could not identify the landing edge for incident segment " +
                        std::to_string(source_sid));
                }
                identity_segment = &scene.segments.at(identity_sid);

                bool landing_at_a = exact_point_equal(
                    vertex.boundary_point, identity_segment->a);
                const bool landing_at_b = exact_point_equal(
                    vertex.boundary_point, identity_segment->b);
                if (!landing_at_a && !landing_at_b) {
                    const double da = squared_distance(
                        vertex.boundary_point, identity_segment->a);
                    const double db = squared_distance(
                        vertex.boundary_point, identity_segment->b);
                    landing_at_a = da <= db;
                }

                const Point landing_delta = subtract(
                    identity_segment->b, identity_segment->a);
                const double landing_length = std::hypot(
                    x(landing_delta), y(landing_delta));
                if (!(landing_length > 0.0)) {
                    throw std::runtime_error(
                        "Cannot slide a collinearity candidate along degenerate landing segment " +
                        std::to_string(identity_sid));
                }

                const Point inward_tangent = landing_at_a
                    ? make_point(x(landing_delta) / landing_length,
                                 y(landing_delta) / landing_length)
                    : make_point(-x(landing_delta) / landing_length,
                                 -y(landing_delta) / landing_length);

                const Point generating_delta = subtract(
                    vertex.boundary_point, vertex.ray_origin);
                const double generating_length = std::hypot(
                    x(generating_delta), y(generating_delta));
                if (!(generating_length > 0.0)) {
                    throw std::runtime_error(
                        "Cannot determine the normal of incident segment " +
                        std::to_string(source_sid));
                }
                const Point generating_normal = make_point(
                    -y(generating_delta) / generating_length,
                    x(generating_delta) / generating_length);
                // The normal line has two orientations. The absolute projection
                // selects the orientation that points from the shared endpoint
                // into the landing edge; movement itself stays tangential.
                const double projection = std::abs(
                    dot(generating_normal, inward_tangent));
                if (projection <= 1e-12) ++near_orthogonal_slides;

                double slide_distance = boundary_epsilon;
                if (slide_distance >= landing_length) {
                    // Stay strictly inside the landing edge. This should only be
                    // reached for an epsilon larger than a very short input edge.
                    slide_distance = 0.5 * landing_length;
                    ++clamped_slides;
                }
                const double delta_t = slide_distance / landing_length;
                parameter = landing_at_a ? delta_t : 1.0 - delta_t;
                parameter = std::clamp(parameter, 0.0, 1.0);
                candidate.point = interpolate_segment(*identity_segment, parameter);
                candidate.is_vertex = slide_distance == 0.0;
                candidate.has_canonical_edge_parameter = true;
                candidate.canonical_edge_segment = identity_sid;
                candidate.canonical_edge_parameter = parameter;
                candidate.is_incident_edge_slide = slide_distance > 0.0;
                candidate.free_space_normal = free_space_unit_normal(scene, identity_sid);
                candidate.incident_segments[0] = identity_sid;
                if (slide_distance == 0.0) {
                    add_incident_segment(candidate, source_sid);
                } else {
                    ++slid;
                }

                interval = {
                    static_cast<long double>(parameter),
                    static_cast<long double>(parameter)};
                prescribed_exact_parameter = VerifierFT(parameter);
            } else {
                const Point direction = subtract(
                    identity_segment->b, identity_segment->a);
                const double denominator = dot(direction, direction);
                if (denominator <= 1e-20) continue;
                parameter = std::clamp(
                    dot(subtract(vertex.boundary_point, identity_segment->a), direction) /
                        denominator,
                    0.0, 1.0);

                // The inexact visibility walk can return a point a few ulps to
                // either side of its generating boundary edge.  Keep the raw
                // point in boundary_anchor for lazy exact identity, but make the
                // actual candidate query a canonical affine point on that edge.
                // This is not tolerance snapping: the generating edge is part of
                // the construction provenance, and only the edge parameter is
                // rounded to the program's double coordinate model.
                candidate.point = interpolate_segment(*identity_segment, parameter);
                candidate.free_space_normal = free_space_unit_normal(scene, identity_sid);
                candidate.has_canonical_edge_parameter = true;
                candidate.canonical_edge_segment = identity_sid;
                candidate.canonical_edge_parameter = parameter;
                candidate.is_vertex = parameter == 0.0 || parameter == 1.0;
                candidate.incident_segments[0] = identity_sid;
                interval = vertex.has_ray_witness
                    ? ray_segment_parameter_interval(
                        *identity_segment,
                        vertex.ray_origin,
                        vertex.ray_witness,
                        vertex.boundary_point)
                    : segment_parameter_interval_from_point(
                        *identity_segment, vertex.boundary_point);
            }

            const double identity_length = distance(
                identity_segment->a, identity_segment->b);
            candidate.polygon_id = identity_segment->polygon_id;
            candidate.ring_id = identity_segment->ring_id;
            candidate.arclength = identity_segment->ring_arclength_begin +
                parameter * identity_length;

            const std::size_t pending_index = pending.size();
            pending.push_back({
                std::move(candidate),
                vertex.ray_origin,
                vertex.ray_witness,
                vertex.has_ray_witness && !vertex.slide_on_landing_edge,
                true});
            identity_groups[group_key(
                identity_sid,
                vertex.slide_on_landing_edge)].push_back({
                    pending_index,
                    std::numeric_limits<std::uint32_t>::max(),
                    interval,
                    std::move(prescribed_exact_parameter)});
        }
    }

    std::size_t exact_duplicates = 0;
    std::size_t interval_overlap_tests = 0;
    std::size_t lazy_exact_evaluations = 0;

    for (auto& [key, nodes] : identity_groups) {
        if (nodes.size() < 2) continue;
        const auto sid = static_cast<std::uint32_t>(key >> 1U);
        const SegmentData& segment = scene.segments.at(sid);

        std::stable_sort(nodes.begin(), nodes.end(), [](const IdentityNode& a,
                                                        const IdentityNode& b) {
            if (a.interval.lo != b.interval.lo) return a.interval.lo < b.interval.lo;
            if (a.interval.hi != b.interval.hi) return a.interval.hi < b.interval.hi;
            const bool a_existing = a.pending_index == no_pending;
            const bool b_existing = b.pending_index == no_pending;
            if (a_existing != b_existing) return a_existing;
            return a.pending_index < b.pending_index;
        });

        std::vector<std::size_t> parent(nodes.size());
        std::iota(parent.begin(), parent.end(), 0U);
        auto find_root = [&](std::size_t index) {
            std::size_t root = index;
            while (parent[root] != root) root = parent[root];
            while (parent[index] != index) {
                const std::size_t next = parent[index];
                parent[index] = root;
                index = next;
            }
            return root;
        };
        auto unite = [&](std::size_t a, std::size_t b) {
            const std::size_t ra = find_root(a);
            const std::size_t rb = find_root(b);
            if (ra != rb) parent[rb] = ra;
        };

        auto exact_parameter = [&](IdentityNode& node) -> const VerifierFT& {
            if (!node.exact_parameter.has_value()) {
                const PendingCandidate& item = pending.at(node.pending_index);
                node.exact_parameter = item.has_ray_witness
                    ? exact_ray_segment_parameter(
                        segment,
                        item.ray_origin,
                        item.ray_witness,
                        item.candidate.boundary_anchor)
                    : exact_segment_parameter_from_point(
                        segment, item.candidate.boundary_anchor);
                ++lazy_exact_evaluations;
            }
            return *node.exact_parameter;
        };

        std::vector<std::size_t> active;
        active.reserve(std::min<std::size_t>(nodes.size(), 64));
        for (std::size_t i = 0; i < nodes.size(); ++i) {
            active.erase(std::remove_if(active.begin(), active.end(), [&](std::size_t j) {
                return nodes[j].interval.hi < nodes[i].interval.lo;
            }), active.end());

            for (const std::size_t j : active) {
                if (nodes[i].interval.hi < nodes[j].interval.lo ||
                    nodes[j].interval.hi < nodes[i].interval.lo) {
                    continue;
                }
                ++interval_overlap_tests;
                if (exact_parameter(nodes[i]) == exact_parameter(nodes[j])) {
                    unite(i, j);
                }
            }
            active.push_back(i);
        }

        struct ComponentChoice {
            bool has_existing = false;
            std::uint32_t existing_sample_id =
                std::numeric_limits<std::uint32_t>::max();
            std::size_t kept_pending = std::numeric_limits<std::size_t>::max();
        };
        std::unordered_map<std::size_t, ComponentChoice> choices;
        choices.reserve(nodes.size());
        for (std::size_t i = 0; i < nodes.size(); ++i) {
            const std::size_t root = find_root(i);
            ComponentChoice& choice = choices[root];
            if (nodes[i].pending_index == no_pending) {
                choice.has_existing = true;
                choice.existing_sample_id = std::min(
                    choice.existing_sample_id, nodes[i].existing_sample_id);
            } else if (choice.kept_pending == no_pending ||
                       nodes[i].pending_index < choice.kept_pending) {
                choice.kept_pending = nodes[i].pending_index;
            }
        }

        for (std::size_t i = 0; i < nodes.size(); ++i) {
            if (nodes[i].pending_index == no_pending) continue;
            const ComponentChoice& choice = choices.at(find_root(i));
            const bool keep = !choice.has_existing &&
                              nodes[i].pending_index == choice.kept_pending;
            if (!keep && pending[nodes[i].pending_index].keep) {
                pending[nodes[i].pending_index].keep = false;
                ++exact_duplicates;
            }
        }
    }

    for (PendingCandidate& item : pending) {
        if (!item.keep) continue;
        append_candidate_sample(scene, std::move(item.candidate));
    }

    std::cout << "Built " << scene.candidates.size()
              << " visibility-polygon vertex candidates; " << slid
              << " incident-edge candidate occurrences were slid along their "
                 "landing edge by " << boundary_epsilon << " m, and "
              << exact_duplicates
              << " mathematically equal same-edge candidate occurrences were "
                 "unified. Lazy identity filtering performed "
              << interval_overlap_tests << " exact-equality probes and materialized "
              << lazy_exact_evaluations << "/" << pending.size()
              << " generated exact parameters.\n";
    if (clamped_slides > 0) {
        std::cout << "Warning: " << clamped_slides
                  << " landing edges were shorter than boundary epsilon; those "
                     "slides were limited to half of the landing-edge length.\n";
    }
    if (near_orthogonal_slides > 0) {
        std::cout << "Note: " << near_orthogonal_slides
                  << " generating-edge normals had a near-zero projection on "
                     "their landing edge; the deterministic inward edge direction "
                     "was used.\n";
    }
    if (nongeneral_incident_counts > 0) {
        std::cout << "Note: " << nongeneral_incident_counts << "/" << source_count
                  << " source visibility polygons had an incident-edge candidate count "
                     "different from two because the input is not in general position.\n";
    }
}

[[nodiscard]] SegmentBreakpoints collect_visibility_breakpoints(
    const Scene& scene,
    const VisibilityEngine& engine,
    std::size_t progress_every,
    std::string_view stage_name,
    double max_vertex_distance = 0.0)
{
    SegmentBreakpoints breakpoints(scene.segments.size());
    constexpr std::size_t block_size = 256;
    const std::size_t candidate_count = scene.candidates.size();
    std::uint64_t total_faces = 0;
    std::uint64_t total_breakpoints = 0;
    const auto start = std::chrono::steady_clock::now();
    std::size_t next_report = progress_every > 0 ? progress_every : candidate_count + 1;

    for (std::size_t base = 0; base < candidate_count; base += block_size) {
        const std::size_t count = std::min(block_size, candidate_count - base);
        std::vector<std::vector<VisibilityEngine::BoundaryBreakpoint>> results(count);
        std::vector<VisibilityEngine::WalkStats> stats(count);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(dynamic, 8)
#endif
        for (std::int64_t local_signed = 0;
             local_signed < static_cast<std::int64_t>(count);
             ++local_signed) {
            const std::size_t local = static_cast<std::size_t>(local_signed);
            results[local] = engine.compute_visibility_breakpoints(
                base + local, max_vertex_distance, &stats[local]);
        }

        for (std::size_t local = 0; local < count; ++local) {
            total_faces += stats[local].faces;
            total_breakpoints += results[local].size();
            for (const auto& breakpoint : results[local]) {
                breakpoints.at(breakpoint.segment_id).push_back(breakpoint.parameter);
            }
        }

        const std::size_t done = base + count;
        if (progress_every > 0 && (done >= next_report || done == candidate_count)) {
            while (next_report <= done) next_report += progress_every;
            const double seconds = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - start).count();
            std::cerr << stage_name << ": " << done << "/" << candidate_count
                      << " visibility polygons, " << std::fixed << std::setprecision(1)
                      << (seconds > 0.0 ? static_cast<double>(done) / seconds : 0.0)
                      << "/s, avg "
                      << (done > 0 ? static_cast<double>(total_faces) / static_cast<double>(done) : 0.0)
                      << " faces and "
                      << (done > 0 ? static_cast<double>(total_breakpoints) / static_cast<double>(done) : 0.0)
                      << " boundary endpoints/polygon.\n";
        }
    }
    return breakpoints;
}

void encode_varint(std::uint32_t value, std::vector<std::uint8_t>& out) {
    while (value >= 0x80U) {
        out.push_back(static_cast<std::uint8_t>((value & 0x7FU) | 0x80U));
        value >>= 7U;
    }
    out.push_back(static_cast<std::uint8_t>(value));
}

[[nodiscard]] std::uint32_t decode_varint(const std::vector<std::uint8_t>& data, std::uint64_t& offset, std::uint64_t end) {
    std::uint32_t result = 0;
    unsigned shift = 0;
    while (offset < end && shift <= 28U) {
        const std::uint8_t byte = data[static_cast<std::size_t>(offset++)];
        result |= static_cast<std::uint32_t>(byte & 0x7FU) << shift;
        if ((byte & 0x80U) == 0U) return result;
        shift += 7U;
    }
    throw std::runtime_error("Corrupt visibility cache varint");
}

void hash_bytes(std::uint64_t& hash, const void* data, std::size_t size) {
    constexpr std::uint64_t prime = 1099511628211ULL;
    const auto* bytes = static_cast<const std::uint8_t*>(data);
    for (std::size_t i = 0; i < size; ++i) {
        hash ^= bytes[i];
        hash *= prime;
    }
}

template <typename T>
void hash_value(std::uint64_t& hash, const T& value) {
    hash_bytes(hash, &value, sizeof(T));
}

[[nodiscard]] std::uint64_t scene_fingerprint(const Scene& scene, const VisibilityConfig& config) {
    std::uint64_t hash = 1469598103934665603ULL;
    constexpr std::uint64_t algorithm_revision = 0x50495045303031ULL; // "PIPE001"
    hash_value(hash, algorithm_revision);
    hash_value(hash, config.bbox_padding);
    const std::uint64_t polygons = scene.polygons.size();
    const std::uint64_t samples = scene.samples.size();
    const std::uint64_t candidates = scene.candidates.size();
    hash_value(hash, polygons); hash_value(hash, samples); hash_value(hash, candidates);
    for (const BoundaryPoint& sample : scene.samples) {
        const double px = x(sample.point), py = y(sample.point);
        hash_value(hash, px); hash_value(hash, py);
        hash_value(hash, sample.polygon_id); hash_value(hash, sample.ring_id);
        hash_value(hash, sample.weight);
        const std::uint8_t flags = static_cast<std::uint8_t>(
            (sample.is_vertex ? 1 : 0) |
            (sample.is_candidate ? 2 : 0) |
            (sample.offset_from_boundary ? 4 : 0) |
            (sample.has_canonical_edge_parameter ? 8 : 0) |
            (sample.is_incident_edge_slide ? 16 : 0));
        hash_value(hash, flags);
        hash_value(hash, sample.canonical_edge_segment);
        hash_value(hash, sample.canonical_edge_parameter);
        hash_value(hash, sample.incident_segments[0]);
        hash_value(hash, sample.incident_segments[1]);
    }
    for (const std::uint32_t id : scene.candidates) hash_value(hash, id);
    return hash;
}

class VisibilityCache {
public:
    void build(const Scene& scene, const VisibilityEngine& engine, std::size_t progress_every) {
        offsets_.assign(scene.candidates.size() + 1, 0);
        data_.clear();
        clear_evaluation_views();
        total_memberships_ = 0;
        const auto start = std::chrono::steady_clock::now();
        std::uint64_t total_faces = 0;
        std::uint64_t total_portals = 0;
        std::uint64_t total_blockers = 0;
        constexpr std::size_t block_size = 256;
        const std::size_t candidate_count = scene.candidates.size();
        std::size_t next_report = progress_every > 0 ? progress_every : candidate_count + 1;

        for (std::size_t base = 0; base < candidate_count; base += block_size) {
            const std::size_t count = std::min(block_size, candidate_count - base);
            std::vector<std::vector<std::uint32_t>> results(count);
            std::vector<VisibilityEngine::WalkStats> stats(count);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(dynamic, 8)
#endif
            for (std::int64_t local_signed = 0; local_signed < static_cast<std::int64_t>(count); ++local_signed) {
                const std::size_t local = static_cast<std::size_t>(local_signed);
                results[local] = engine.compute(base + local, &stats[local]);
            }

            for (std::size_t local = 0; local < count; ++local) {
                const std::size_t ci = base + local;
                offsets_[ci] = data_.size();
                const auto& visible = results[local];
                total_memberships_ += visible.size();
                total_faces += stats[local].faces;
                total_portals += stats[local].portals;
                total_blockers += stats[local].blockers;
                std::uint32_t previous = 0;
                bool first = true;
                for (const std::uint32_t id : visible) {
                    encode_varint(first ? id : id - previous, data_);
                    previous = id;
                    first = false;
                }
            }

            const std::size_t done = base + count;
            if (progress_every > 0 && (done >= next_report || done == candidate_count)) {
                while (next_report <= done) next_report += progress_every;
                const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
                const double denom = static_cast<double>(done);
                std::cerr << "Visibility cache: " << done << "/" << candidate_count
                          << " candidates, " << std::fixed << std::setprecision(1)
                          << (seconds > 0.0 ? denom / seconds : 0.0) << "/s, avg "
                          << (denom > 0.0 ? static_cast<double>(total_faces) / denom : 0.0) << " faces, "
                          << (denom > 0.0 ? static_cast<double>(total_blockers) / denom : 0.0) << " blockers, "
                          << (denom > 0.0 ? static_cast<double>(total_memberships_) / denom : 0.0)
                          << " visible samples/candidate, cache "
                          << std::setprecision(2) << static_cast<double>(data_.size()) / (1024.0 * 1024.0) << " MiB\n";
            }
        }
        offsets_.back() = data_.size();
    }

    void prepare_for_evaluation(std::size_t k, bool expand_in_memory, bool deduplicate) {
        clear_evaluation_views();
        if (expand_in_memory) expand();
        prepare_candidate_mask(k, deduplicate);
    }

    template <typename Fn>
    void for_each(std::size_t candidate_index, Fn&& fn) const {
        if (expanded_ready_) {
            const std::uint64_t begin = expanded_offsets_.at(candidate_index);
            const std::uint64_t end = expanded_offsets_.at(candidate_index + 1);
            for (std::uint64_t i = begin; i < end; ++i) fn(expanded_samples_[static_cast<std::size_t>(i)]);
            return;
        }
        std::uint64_t offset = offsets_.at(candidate_index);
        const std::uint64_t end = offsets_.at(candidate_index + 1);
        std::uint32_t current = 0;
        bool first = true;
        while (offset < end) {
            const std::uint32_t delta = decode_varint(data_, offset, end);
            current = first ? delta : current + delta;
            first = false;
            fn(current);
        }
    }

    [[nodiscard]] bool is_search_candidate(std::size_t candidate_index) const {
        return search_candidate_mask_.empty() || search_candidate_mask_.at(candidate_index) != 0;
    }
    [[nodiscard]] std::size_t search_candidate_count() const {
        return search_candidate_mask_.empty() ? size() : search_candidate_count_;
    }
    [[nodiscard]] std::size_t duplicate_candidate_count() const { return duplicate_candidate_count_; }
    [[nodiscard]] bool expanded() const { return expanded_ready_; }
    [[nodiscard]] std::size_t expanded_byte_size() const {
        return expanded_offsets_.size() * sizeof(std::uint64_t) +
            expanded_sample_count_ * sizeof(std::uint32_t);
    }
    [[nodiscard]] std::size_t size() const { return offsets_.empty() ? 0 : offsets_.size() - 1; }
    [[nodiscard]] std::size_t byte_size() const { return data_.size() + offsets_.size() * sizeof(std::uint64_t); }
    [[nodiscard]] std::uint64_t total_memberships() const { return total_memberships_; }

    void save(const fs::path& path, const Scene& scene, const VisibilityConfig& config) const {
        if (path.empty()) return;
        if (path.has_parent_path()) fs::create_directories(path.parent_path());
        fs::path temporary = path;
        temporary += ".tmp";
        std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
        if (!out) throw std::runtime_error("Could not write cache: " + temporary.string());
        const char magic[8] = {'G','I','S','V','I','S','3','\0'};
        const std::uint32_t endian = 0x01020304U;
        const std::uint32_t format_version = 3;
        const std::uint64_t fingerprint = scene_fingerprint(scene, config);
        const std::uint64_t cgal_version = CGAL_VERSION_NR;
        const std::uint64_t candidates = scene.candidates.size();
        const std::uint64_t samples = scene.samples.size();
        const std::uint64_t offsets_count = offsets_.size();
        const std::uint64_t data_count = data_.size();
        out.write(magic, 8);
        out.write(reinterpret_cast<const char*>(&endian), sizeof(endian));
        out.write(reinterpret_cast<const char*>(&format_version), sizeof(format_version));
        out.write(reinterpret_cast<const char*>(&fingerprint), sizeof(fingerprint));
        out.write(reinterpret_cast<const char*>(&cgal_version), sizeof(cgal_version));
        out.write(reinterpret_cast<const char*>(&candidates), sizeof(candidates));
        out.write(reinterpret_cast<const char*>(&samples), sizeof(samples));
        out.write(reinterpret_cast<const char*>(&offsets_count), sizeof(offsets_count));
        out.write(reinterpret_cast<const char*>(&data_count), sizeof(data_count));
        out.write(reinterpret_cast<const char*>(&total_memberships_), sizeof(total_memberships_));
        out.write(reinterpret_cast<const char*>(offsets_.data()), static_cast<std::streamsize>(offsets_.size() * sizeof(std::uint64_t)));
        out.write(reinterpret_cast<const char*>(data_.data()), static_cast<std::streamsize>(data_.size()));
        out.close();
        if (!out) throw std::runtime_error("Failed while writing cache: " + temporary.string());
        if (fs::exists(path)) fs::remove(path);
        fs::rename(temporary, path);
    }

    [[nodiscard]] bool load(const fs::path& path, const Scene& scene, const VisibilityConfig& config) {
        if (path.empty() || !fs::exists(path)) return false;
        std::ifstream in(path, std::ios::binary);
        if (!in) return false;
        char magic[8]{};
        std::uint32_t endian = 0, format_version = 0;
        std::uint64_t fingerprint = 0, cgal_version = 0, candidates = 0, samples = 0;
        std::uint64_t offsets_count = 0, data_count = 0;
        in.read(magic, 8);
        in.read(reinterpret_cast<char*>(&endian), sizeof(endian));
        in.read(reinterpret_cast<char*>(&format_version), sizeof(format_version));
        in.read(reinterpret_cast<char*>(&fingerprint), sizeof(fingerprint));
        in.read(reinterpret_cast<char*>(&cgal_version), sizeof(cgal_version));
        in.read(reinterpret_cast<char*>(&candidates), sizeof(candidates));
        in.read(reinterpret_cast<char*>(&samples), sizeof(samples));
        in.read(reinterpret_cast<char*>(&offsets_count), sizeof(offsets_count));
        in.read(reinterpret_cast<char*>(&data_count), sizeof(data_count));
        in.read(reinterpret_cast<char*>(&total_memberships_), sizeof(total_memberships_));
        if (!in || std::memcmp(magic, "GISVIS3", 7) != 0 || endian != 0x01020304U || format_version != 3 ||
            fingerprint != scene_fingerprint(scene, config) || cgal_version != static_cast<std::uint64_t>(CGAL_VERSION_NR) ||
            candidates != scene.candidates.size() || samples != scene.samples.size() || offsets_count != candidates + 1) {
            return false;
        }
        offsets_.resize(static_cast<std::size_t>(offsets_count));
        data_.resize(static_cast<std::size_t>(data_count));
        in.read(reinterpret_cast<char*>(offsets_.data()), static_cast<std::streamsize>(offsets_.size() * sizeof(std::uint64_t)));
        in.read(reinterpret_cast<char*>(data_.data()), static_cast<std::streamsize>(data_.size()));
        if (!in || offsets_.empty() || offsets_.front() != 0 || offsets_.back() != data_.size()) return false;
        for (std::size_t i = 1; i < offsets_.size(); ++i) {
            if (offsets_[i] < offsets_[i - 1] || offsets_[i] > data_.size()) return false;
        }
        clear_evaluation_views();
        return true;
    }

private:
    void clear_evaluation_views() {
        expanded_offsets_.clear();
        expanded_samples_.reset();
        expanded_sample_count_ = 0;
        expanded_ready_ = false;
        search_candidate_mask_.clear();
        search_candidate_count_ = 0;
        duplicate_candidate_count_ = 0;
    }

    void expand() {
        const auto start = std::chrono::steady_clock::now();
        const std::size_t candidate_count = size();
        expanded_offsets_.assign(candidate_count + 1, 0);
        expanded_samples_.reset();
        expanded_sample_count_ = 0;

        // Pass 1 counts row memberships independently. A varint ends at every
        // byte whose continuation bit is clear, so this pass does not need to
        // reconstruct sample IDs and is limited mainly by compressed-cache
        // memory bandwidth.
        std::vector<std::uint64_t> row_counts(candidate_count, 0);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(static)
#endif
        for (std::int64_t candidate_signed = 0;
             candidate_signed < static_cast<std::int64_t>(candidate_count);
             ++candidate_signed) {
            const std::size_t candidate = static_cast<std::size_t>(candidate_signed);
            const std::uint64_t begin = offsets_[candidate];
            const std::uint64_t end = offsets_[candidate + 1];
            std::uint64_t count = 0;
            for (std::uint64_t offset = begin; offset < end; ++offset) {
                if ((data_[static_cast<std::size_t>(offset)] & 0x80U) == 0U) ++count;
            }
            row_counts[candidate] = count;
        }

        for (std::size_t candidate = 0; candidate < candidate_count; ++candidate) {
            expanded_offsets_[candidate + 1] =
                expanded_offsets_[candidate] + row_counts[candidate];
        }
        const std::uint64_t decoded_memberships = expanded_offsets_.back();
        if (decoded_memberships != total_memberships_) {
            throw std::runtime_error(
                "Compressed visibility cache membership count does not match its header");
        }
        if (decoded_memberships >
            static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
            throw std::runtime_error(
                "Expanded visibility cache is too large for this address space");
        }

        expanded_sample_count_ = static_cast<std::size_t>(decoded_memberships);
        expanded_samples_ =
            std::make_unique_for_overwrite<std::uint32_t[]>(expanded_sample_count_);

        // Pass 2 decodes each row directly into its disjoint CSR slice. No
        // locks or atomics are needed for output writes.
        std::atomic<bool> decode_failed{false};
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(dynamic, 32)
#endif
        for (std::int64_t candidate_signed = 0;
             candidate_signed < static_cast<std::int64_t>(candidate_count);
             ++candidate_signed) {
            if (decode_failed.load(std::memory_order_relaxed)) continue;
            const std::size_t candidate = static_cast<std::size_t>(candidate_signed);
            std::uint64_t input_offset = offsets_[candidate];
            const std::uint64_t input_end = offsets_[candidate + 1];
            std::uint64_t output_offset = expanded_offsets_[candidate];
            const std::uint64_t output_end = expanded_offsets_[candidate + 1];
            std::uint32_t current = 0;
            bool first = true;
            try {
                while (input_offset < input_end) {
                    const std::uint32_t delta =
                        decode_varint(data_, input_offset, input_end);
                    current = first ? delta : current + delta;
                    first = false;
                    if (output_offset >= output_end) {
                        decode_failed.store(true, std::memory_order_relaxed);
                        break;
                    }
                    expanded_samples_[static_cast<std::size_t>(output_offset++)] = current;
                }
                if (output_offset != output_end) {
                    decode_failed.store(true, std::memory_order_relaxed);
                }
            } catch (...) {
                decode_failed.store(true, std::memory_order_relaxed);
            }
        }
        if (decode_failed.load(std::memory_order_relaxed)) {
            expanded_samples_.reset();
            expanded_sample_count_ = 0;
            expanded_offsets_.clear();
            throw std::runtime_error("Corrupt visibility cache while expanding CSR rows");
        }

        expanded_ready_ = true;
        const double seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - start).count();
        std::cout << "Expanded visibility cache to CSR with parallel row decoding in "
                  << seconds << " s (" << std::fixed << std::setprecision(2)
                  << static_cast<double>(expanded_byte_size()) / (1024.0 * 1024.0)
                  << " MiB evaluation memory).\n";
    }

    [[nodiscard]] std::uint64_t encoded_row_hash(std::size_t candidate) const {
        std::uint64_t hash = 1469598103934665603ULL;
        const std::uint64_t begin = offsets_[candidate];
        const std::uint64_t end = offsets_[candidate + 1];
        const std::uint64_t length = end - begin;
        hash_value(hash, length);
        if (length > 0) hash_bytes(hash, data_.data() + static_cast<std::size_t>(begin), static_cast<std::size_t>(length));
        return hash;
    }

    [[nodiscard]] bool encoded_rows_equal(std::size_t a, std::size_t b) const {
        const std::uint64_t a_begin = offsets_[a], a_end = offsets_[a + 1];
        const std::uint64_t b_begin = offsets_[b], b_end = offsets_[b + 1];
        const std::uint64_t a_length = a_end - a_begin;
        const std::uint64_t b_length = b_end - b_begin;
        return a_length == b_length &&
            (a_length == 0 || std::memcmp(data_.data() + static_cast<std::size_t>(a_begin), data_.data() + static_cast<std::size_t>(b_begin),
                                          static_cast<std::size_t>(a_length)) == 0);
    }

    void prepare_candidate_mask(std::size_t k, bool deduplicate) {
        search_candidate_mask_.assign(size(), 1);
        search_candidate_count_ = size();
        duplicate_candidate_count_ = 0;
        if (!deduplicate || size() == 0) return;

        const auto hash_start = std::chrono::steady_clock::now();
        const std::size_t candidate_count = size();
        std::vector<std::uint64_t> row_hashes(candidate_count, 0);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(static)
#endif
        for (std::int64_t candidate_signed = 0;
             candidate_signed < static_cast<std::int64_t>(candidate_count);
             ++candidate_signed) {
            const std::size_t candidate =
                static_cast<std::size_t>(candidate_signed);
            row_hashes[candidate] = encoded_row_hash(candidate);
        }
        const double hash_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - hash_start).count();

        const auto group_start = std::chrono::steady_clock::now();
        std::unordered_map<std::uint64_t, std::vector<std::size_t>> buckets;
        buckets.reserve(size() * 2);
        std::vector<std::uint8_t> representative(size(), 1);
        std::size_t unique_count = 0;
        for (std::size_t candidate = 0; candidate < size(); ++candidate) {
            auto& bucket = buckets[row_hashes[candidate]];
            bool duplicate = false;
            for (const std::size_t prior : bucket) {
                if (encoded_rows_equal(candidate, prior)) {
                    duplicate = true;
                    representative[candidate] = 0;
                    break;
                }
            }
            if (!duplicate) {
                bucket.push_back(candidate);
                ++unique_count;
            }
        }
        const double group_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - group_start).count();
        std::cout << "Prepared exact-row deduplication: parallel hashing "
                  << hash_seconds << " s, deterministic grouping "
                  << group_seconds << " s.\n";

        if (unique_count < k) {
            std::cout << "Found " << (size() - unique_count)
                      << " exact duplicate visibility rows, but kept all candidates because only "
                      << unique_count << " unique rows remain for k=" << k << ".\n";
            return;
        }
        search_candidate_mask_ = std::move(representative);
        search_candidate_count_ = unique_count;
        duplicate_candidate_count_ = size() - unique_count;
        if (duplicate_candidate_count_ > 0) {
            std::cout << "Removed " << duplicate_candidate_count_
                      << " objective-equivalent candidate(s) with identical visibility rows; "
                      << search_candidate_count_ << " representatives remain.\n";
        }
    }

    std::vector<std::uint64_t> offsets_;
    std::vector<std::uint8_t> data_;
    std::vector<std::uint64_t> expanded_offsets_;
    std::unique_ptr<std::uint32_t[]> expanded_samples_;
    std::size_t expanded_sample_count_ = 0;
    std::vector<std::uint8_t> search_candidate_mask_;
    std::uint64_t total_memberships_ = 0;
    std::size_t search_candidate_count_ = 0;
    std::size_t duplicate_candidate_count_ = 0;
    bool expanded_ready_ = false;
};


struct CandidatePolygonMap {
    // Distinct polygons touched by each candidate's visibility row.  There is
    // deliberately no reverse polygon-to-candidate index: hotness is decided by
    // comparing these polygon epochs with the candidate's own last-check epoch.
    std::vector<std::vector<std::uint32_t>> polygons_by_candidate;
    std::size_t membership_count = 0;
};

[[nodiscard]] CandidatePolygonMap build_candidate_polygon_map(
    const Scene& scene,
    const VisibilityCache& cache,
    bool verbose)
{
    const auto start = std::chrono::steady_clock::now();
    const std::size_t candidate_count = scene.candidates.size();
    const std::size_t polygon_count = scene.polygons.size();

    CandidatePolygonMap map;
    map.polygons_by_candidate.resize(candidate_count);

#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel
#endif
    {
        std::vector<std::uint32_t> polygon_stamp(polygon_count, 0);
        std::vector<std::uint32_t> touched;
        std::uint32_t epoch = 1;
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp for schedule(dynamic, 64)
#endif
        for (std::int64_t ci_signed = 0;
             ci_signed < static_cast<std::int64_t>(candidate_count);
             ++ci_signed) {
            const std::size_t ci = static_cast<std::size_t>(ci_signed);
            if (!cache.is_search_candidate(ci)) continue;
            if (++epoch == 0) {
                std::fill(polygon_stamp.begin(), polygon_stamp.end(), 0);
                epoch = 1;
            }
            touched.clear();
            cache.for_each(ci, [&](std::uint32_t sample_id) {
                const std::uint32_t polygon_id = scene.samples[sample_id].polygon_id;
                if (polygon_stamp[polygon_id] == epoch) return;
                polygon_stamp[polygon_id] = epoch;
                touched.push_back(polygon_id);
            });
            std::sort(touched.begin(), touched.end());
            map.polygons_by_candidate[ci] = touched;
        }
    }

    std::uint64_t membership_count = 0;
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for reduction(+:membership_count) schedule(static)
#endif
    for (std::int64_t candidate_signed = 0;
         candidate_signed < static_cast<std::int64_t>(candidate_count);
         ++candidate_signed) {
        membership_count += map.polygons_by_candidate[
            static_cast<std::size_t>(candidate_signed)].size();
    }
    map.membership_count = static_cast<std::size_t>(membership_count);

    if (verbose) {
        const double seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - start).count();
        const double average = cache.search_candidate_count() > 0
            ? static_cast<double>(map.membership_count) /
                  static_cast<double>(cache.search_candidate_count())
            : 0.0;
        std::cout << "Built candidate-visible-polygon map: "
                  << map.membership_count << " candidate-polygon memberships, avg "
                  << average << " visible polygons/search candidate in "
                  << seconds << " s (no reverse polygon index).\n";
    }
    return map;
}

[[nodiscard]] bool candidate_is_hot(
    std::size_t candidate,
    const CandidatePolygonMap& candidate_polygon_map,
    const std::vector<std::uint64_t>& polygon_changed_round,
    const std::vector<std::uint64_t>& candidate_checked_round)
{
    const std::uint64_t checked_round = candidate_checked_round.at(candidate);
    if (checked_round == 0) return true;
    for (const std::uint32_t polygon_id :
         candidate_polygon_map.polygons_by_candidate.at(candidate)) {
        if (polygon_changed_round.at(polygon_id) > checked_round) return true;
    }
    return false;
}


struct CandidatePolygonReverseIndex {
    // Used only for bounded anchor-partner subsampling. Hot/cold 1-swap
    // invalidation remains forward-only and never walks these lists.
    std::vector<std::vector<std::uint32_t>> candidates_by_polygon;
    std::size_t membership_count = 0;
};

[[nodiscard]] CandidatePolygonReverseIndex build_candidate_polygon_reverse_index(
    const Scene& scene,
    const VisibilityCache& cache,
    const CandidatePolygonMap& candidate_polygon_map,
    bool verbose)
{
    const auto start = std::chrono::steady_clock::now();
    CandidatePolygonReverseIndex index;
    index.candidates_by_polygon.resize(scene.polygons.size());
    for (std::size_t candidate = 0; candidate < scene.candidates.size(); ++candidate) {
        if (!cache.is_search_candidate(candidate)) continue;
        for (const std::uint32_t polygon_id :
             candidate_polygon_map.polygons_by_candidate.at(candidate)) {
            index.candidates_by_polygon.at(polygon_id).push_back(
                static_cast<std::uint32_t>(candidate));
            ++index.membership_count;
        }
    }
    if (verbose) {
        const double seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - start).count();
        std::cout << "Built bounded-anchor reverse lookup: "
                  << index.membership_count << " candidate-polygon memberships in "
                  << seconds << " s. It is not used by hot/cold 1-swap invalidation.\n";
    }
    return index;
}

[[nodiscard]] GDALDatasetPtr create_geojson(const fs::path& path) {
    if (fs::exists(path)) fs::remove(path);
    GDALDriver* driver = GetGDALDriverManager()->GetDriverByName("GeoJSON");
    if (driver == nullptr) throw std::runtime_error("GDAL GeoJSON driver is unavailable");
    return GDALDatasetPtr(driver->Create(path.string().c_str(), 0, 0, 0, GDT_Unknown, nullptr));
}

void add_field(OGRLayer* layer, const char* name, OGRFieldType type, int width = 0, int precision = 0) {
    OGRFieldDefn field(name, type);
    if (width > 0) field.SetWidth(width);
    if (precision > 0) field.SetPrecision(precision);
    if (layer->CreateField(&field) != OGRERR_NONE) throw std::runtime_error(std::string("Could not create field ") + name);
}

void write_samples(const Scene& scene, const fs::path& output_dir) {
    const fs::path path = output_dir / "boundary_samples.geojson";
    auto dataset = create_geojson(path);
    char** layer_options = nullptr;
    layer_options = CSLSetNameValue(layer_options, "RFC7946", "NO");
    OGRLayer* layer = dataset->CreateLayer("boundary_samples", scene.srs.get(), wkbPoint, layer_options);
    CSLDestroy(layer_options);
    if (layer == nullptr) throw std::runtime_error("Could not create sample layer");
    add_field(layer, "sample_id", OFTInteger64);
    add_field(layer, "polygon_id", OFTInteger);
    add_field(layer, "weight_m", OFTReal, 0, 6);
    add_field(layer, "vertex", OFTInteger);
    add_field(layer, "candidate", OFTInteger);
    add_field(layer, "offset", OFTInteger);
    add_field(layer, "edge_slide", OFTInteger);
    add_field(layer, "edge_sid", OFTInteger64);
    add_field(layer, "edge_t", OFTReal, 0, 12);
    add_field(layer, "anchor_x", OFTReal, 0, 9);
    add_field(layer, "anchor_y", OFTReal, 0, 9);
    add_field(layer, "normal_x", OFTReal, 0, 9);
    add_field(layer, "normal_y", OFTReal, 0, 9);
    add_field(layer, "offset_m", OFTReal, 0, 9);
    for (std::size_t i = 0; i < scene.samples.size(); ++i) {
        const auto& sample = scene.samples[i];
        OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
        feature->SetField("sample_id", static_cast<GIntBig>(i));
        feature->SetField("polygon_id", static_cast<int>(sample.polygon_id));
        feature->SetField("weight_m", sample.weight);
        feature->SetField("vertex", sample.is_vertex ? 1 : 0);
        feature->SetField("candidate", sample.is_candidate ? 1 : 0);
        feature->SetField("offset", sample.offset_from_boundary ? 1 : 0);
        feature->SetField(
            "edge_slide", sample.is_incident_edge_slide ? 1 : 0);
        if (sample.has_canonical_edge_parameter) {
            feature->SetField(
                "edge_sid", static_cast<GIntBig>(sample.canonical_edge_segment));
            feature->SetField("edge_t", sample.canonical_edge_parameter);
        }
        feature->SetField("anchor_x", x(sample.boundary_anchor));
        feature->SetField("anchor_y", y(sample.boundary_anchor));
        feature->SetField("normal_x", x(sample.free_space_normal));
        feature->SetField("normal_y", y(sample.free_space_normal));
        feature->SetField("offset_m", distance(sample.point, sample.boundary_anchor));
        OGRPoint point(x(sample.point), y(sample.point));
        feature->SetGeometry(&point);
        if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write sample feature");
    }
}

void write_domain(const Scene& scene, const Box& box, const fs::path& output_dir) {
    const fs::path path = output_dir / "visibility_domain.geojson";
    auto dataset = create_geojson(path);
    char** options = nullptr;
    options = CSLSetNameValue(options, "RFC7946", "NO");
    OGRLayer* layer = dataset->CreateLayer("visibility_domain", scene.srs.get(), wkbLineString, options);
    CSLDestroy(options);
    if (layer == nullptr) throw std::runtime_error("Could not create visibility-domain layer");
    add_field(layer, "role", OFTString, 32);
    add_field(layer, "objective", OFTInteger);
    OGRLineString line;
    const double min_x = x(box.min_corner()), min_y = y(box.min_corner());
    const double max_x = x(box.max_corner()), max_y = y(box.max_corner());
    line.addPoint(min_x, min_y); line.addPoint(max_x, min_y); line.addPoint(max_x, max_y);
    line.addPoint(min_x, max_y); line.addPoint(min_x, min_y);
    OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
    feature->SetField("role", "outer_visibility_boundary");
    feature->SetField("objective", 0);
    feature->SetGeometry(&line);
    if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write visibility domain");
}

struct MoveInfo {
    std::size_t move_number = 0;
    std::string type;
    std::size_t cardinality = 0;
    std::size_t added_candidate = std::numeric_limits<std::size_t>::max();
    std::size_t removed_candidate = std::numeric_limits<std::size_t>::max();
    std::size_t changed_slot = std::numeric_limits<std::size_t>::max();
    int score = 0;
    double covered_total = 0.0;
};

void write_step_snapshot(
    const Scene& scene,
    const VisibilityEngine& engine,
    const fs::path& output_dir,
    const MoveInfo& move,
    const std::vector<std::size_t>& selected_candidates,
    const std::vector<std::vector<Point>>& visibility_polygons,
    const std::vector<double>& covered_weight,
    double final_threshold,
    std::size_t k,
    double curve_exponent)
{
    std::ostringstream suffix_stream;
    suffix_stream << std::setw(4) << std::setfill('0') << move.move_number;
    const std::string suffix = suffix_stream.str();
    const GIntBig none = static_cast<GIntBig>(-1);
    const GIntBig added = move.added_candidate == std::numeric_limits<std::size_t>::max() ? none : static_cast<GIntBig>(move.added_candidate);
    const GIntBig removed = move.removed_candidate == std::numeric_limits<std::size_t>::max() ? none : static_cast<GIntBig>(move.removed_candidate);

    {
        auto dataset = create_geojson(output_dir / ("selected_step_" + suffix + ".geojson"));
        char** options = nullptr; options = CSLSetNameValue(options, "RFC7946", "NO");
        OGRLayer* layer = dataset->CreateLayer("selected", scene.srs.get(), wkbPoint, options); CSLDestroy(options);
        if (!layer) throw std::runtime_error("Could not create selected-point layer");
        add_field(layer, "slot", OFTInteger); add_field(layer, "candidate", OFTInteger64);
        add_field(layer, "sample_id", OFTInteger64); add_field(layer, "polygon_id", OFTInteger);
        add_field(layer, "is_latest", OFTInteger); add_field(layer, "move_no", OFTInteger);
        add_field(layer, "move_type", OFTString, 12); add_field(layer, "cardinality", OFTInteger);
        add_field(layer, "added", OFTInteger64); add_field(layer, "removed", OFTInteger64);
        add_field(layer, "score", OFTInteger); add_field(layer, "curve_m", OFTReal, 0, 6);
        add_field(layer, "x", OFTReal, 0, 3); add_field(layer, "y", OFTReal, 0, 3);
        for (std::size_t slot = 0; slot < selected_candidates.size(); ++slot) {
            const std::size_t ci = selected_candidates[slot];
            const std::uint32_t sample_id = scene.candidates[ci];
            const auto& sample = scene.samples[sample_id];
            OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
            feature->SetField("slot", static_cast<int>(slot + 1)); feature->SetField("candidate", static_cast<GIntBig>(ci));
            feature->SetField("sample_id", static_cast<GIntBig>(sample_id)); feature->SetField("polygon_id", static_cast<int>(sample.polygon_id));
            feature->SetField("is_latest", slot == move.changed_slot ? 1 : 0); feature->SetField("move_no", static_cast<int>(move.move_number));
            feature->SetField("move_type", move.type.c_str()); feature->SetField("cardinality", static_cast<int>(move.cardinality));
            feature->SetField("added", added); feature->SetField("removed", removed); feature->SetField("score", move.score);
            feature->SetField("curve_m", curve_exponent);
            feature->SetField("x", x(sample.point)); feature->SetField("y", y(sample.point));
            OGRPoint point(x(sample.point), y(sample.point)); feature->SetGeometry(&point);
            if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write selected point");
        }
    }

    {
        auto dataset = create_geojson(output_dir / ("visibility_step_" + suffix + ".geojson"));
        char** options = nullptr; options = CSLSetNameValue(options, "RFC7946", "NO");
        OGRLayer* layer = dataset->CreateLayer("visibility", scene.srs.get(), wkbPolygon, options); CSLDestroy(options);
        if (!layer) throw std::runtime_error("Could not create visibility layer");
        add_field(layer, "slot", OFTInteger); add_field(layer, "candidate", OFTInteger64);
        add_field(layer, "sample_id", OFTInteger64); add_field(layer, "polygon_id", OFTInteger);
        add_field(layer, "is_latest", OFTInteger); add_field(layer, "move_no", OFTInteger);
        add_field(layer, "move_type", OFTString, 12); add_field(layer, "algorithm", OFTString, 32);
        add_field(layer, "regularized", OFTInteger); add_field(layer, "kernel", OFTString, 24);
        add_field(layer, "view_x", OFTReal, 0, 9); add_field(layer, "view_y", OFTReal, 0, 9);
        add_field(layer, "query_offset_m", OFTReal, 0, 9); add_field(layer, "curve_m", OFTReal, 0, 6);
        if (visibility_polygons.size() != selected_candidates.size()) throw std::runtime_error("Visibility polygon count mismatch");
        for (std::size_t slot = 0; slot < selected_candidates.size(); ++slot) {
            const auto& points = visibility_polygons[slot];
            if (points.size() < 4) continue;
            OGRLinearRing ring; for (const Point& p : points) ring.addPoint(x(p), y(p));
            OGRPolygon polygon; polygon.addRing(&ring);
            const std::size_t ci = selected_candidates[slot]; const std::uint32_t sample_id = scene.candidates[ci];
            OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
            feature->SetField("slot", static_cast<int>(slot + 1)); feature->SetField("candidate", static_cast<GIntBig>(ci));
            feature->SetField("sample_id", static_cast<GIntBig>(sample_id));
            feature->SetField("polygon_id", static_cast<int>(scene.samples[sample_id].polygon_id));
            feature->SetField("is_latest", slot == move.changed_slot ? 1 : 0); feature->SetField("move_no", static_cast<int>(move.move_number));
            feature->SetField("move_type", move.type.c_str()); feature->SetField("algorithm", "exact boundary CDT sectors");
            feature->SetField("regularized", 0); feature->SetField("kernel", "EPICK/double");
            const Point view = engine.query_point(ci);
            feature->SetField("view_x", x(view)); feature->SetField("view_y", y(view));
            feature->SetField("query_offset_m", engine.query_offset(ci));
            feature->SetField("curve_m", curve_exponent);
            feature->SetGeometry(&polygon);
            if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write visibility polygon");
        }
    }

    const double ratio = static_cast<double>(move.cardinality) / static_cast<double>(k);
    const double step_target = final_threshold * std::pow(ratio, curve_exponent);

    {
        auto dataset = create_geojson(output_dir / ("coverage_step_" + suffix + ".geojson"));
        char** options = nullptr; options = CSLSetNameValue(options, "RFC7946", "NO");
        OGRLayer* layer = dataset->CreateLayer("coverage", scene.srs.get(), wkbPolygon, options); CSLDestroy(options);
        if (!layer) throw std::runtime_error("Could not create coverage layer");
        add_field(layer, "polygon_id", OFTInteger); add_field(layer, "source_fid", OFTInteger64);
        add_field(layer, "visible_m", OFTReal, 0, 4); add_field(layer, "perimeter_m", OFTReal, 0, 4);
        add_field(layer, "fraction", OFTReal, 0, 6); add_field(layer, "step_target", OFTReal, 0, 6);
        add_field(layer, "margin", OFTReal, 0, 6); add_field(layer, "passes", OFTInteger);
        add_field(layer, "move_no", OFTInteger); add_field(layer, "move_type", OFTString, 12);
        add_field(layer, "cardinality", OFTInteger); add_field(layer, "added", OFTInteger64);
        add_field(layer, "removed", OFTInteger64); add_field(layer, "score", OFTInteger);
        add_field(layer, "curve_m", OFTReal, 0, 6);
        for (const auto& polygon : scene.polygons) {
            OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
            const double fraction = polygon.perimeter > 0.0 ? covered_weight[polygon.internal_id] / polygon.perimeter : 0.0;
            feature->SetField("polygon_id", static_cast<int>(polygon.internal_id)); feature->SetField("source_fid", static_cast<GIntBig>(polygon.source_fid));
            feature->SetField("visible_m", covered_weight[polygon.internal_id]); feature->SetField("perimeter_m", polygon.perimeter);
            feature->SetField("fraction", fraction); feature->SetField("step_target", step_target);
            feature->SetField("margin", fraction - step_target);
            feature->SetField("passes", fraction + 1e-10 >= step_target ? 1 : 0); feature->SetField("move_no", static_cast<int>(move.move_number));
            feature->SetField("move_type", move.type.c_str()); feature->SetField("cardinality", static_cast<int>(move.cardinality));
            feature->SetField("added", added); feature->SetField("removed", removed); feature->SetField("score", move.score);
            feature->SetField("curve_m", curve_exponent);
            feature->SetGeometry(polygon.geometry.get());
            if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write coverage polygon");
        }
    }

    {
        auto dataset = create_geojson(output_dir / ("qualified_step_" + suffix + ".geojson"));
        char** options = nullptr; options = CSLSetNameValue(options, "RFC7946", "NO");
        OGRLayer* layer = dataset->CreateLayer("qualified", scene.srs.get(), wkbPolygon, options); CSLDestroy(options);
        if (!layer) throw std::runtime_error("Could not create above-threshold polygon layer");
        add_field(layer, "polygon_id", OFTInteger); add_field(layer, "source_fid", OFTInteger64);
        add_field(layer, "visible_m", OFTReal, 0, 4); add_field(layer, "perimeter_m", OFTReal, 0, 4);
        add_field(layer, "fraction", OFTReal, 0, 6); add_field(layer, "step_target", OFTReal, 0, 6);
        add_field(layer, "margin", OFTReal, 0, 6); add_field(layer, "visible_pct", OFTReal, 0, 2);
        add_field(layer, "target_pct", OFTReal, 0, 2); add_field(layer, "move_no", OFTInteger);
        add_field(layer, "move_type", OFTString, 12); add_field(layer, "cardinality", OFTInteger);
        add_field(layer, "added", OFTInteger64); add_field(layer, "removed", OFTInteger64);
        add_field(layer, "score", OFTInteger); add_field(layer, "curve_m", OFTReal, 0, 6);
        for (const auto& polygon : scene.polygons) {
            const double fraction = polygon.perimeter > 0.0 ? covered_weight[polygon.internal_id] / polygon.perimeter : 0.0;
            if (fraction + 1e-10 < step_target) continue;
            OGRFeaturePtr feature(OGRFeature::CreateFeature(layer->GetLayerDefn()));
            feature->SetField("polygon_id", static_cast<int>(polygon.internal_id)); feature->SetField("source_fid", static_cast<GIntBig>(polygon.source_fid));
            feature->SetField("visible_m", covered_weight[polygon.internal_id]); feature->SetField("perimeter_m", polygon.perimeter);
            feature->SetField("fraction", fraction); feature->SetField("step_target", step_target);
            feature->SetField("margin", fraction - step_target);
            feature->SetField("visible_pct", 100.0 * fraction); feature->SetField("target_pct", 100.0 * step_target);
            feature->SetField("move_no", static_cast<int>(move.move_number)); feature->SetField("move_type", move.type.c_str());
            feature->SetField("cardinality", static_cast<int>(move.cardinality)); feature->SetField("added", added);
            feature->SetField("removed", removed); feature->SetField("score", move.score);
            feature->SetField("curve_m", curve_exponent);
            feature->SetGeometry(polygon.geometry.get());
            if (layer->CreateFeature(feature.get()) != OGRERR_NONE) throw std::runtime_error("Could not write above-threshold polygon");
        }
    }
}

[[nodiscard]] int solution_score(const Scene& scene, const std::vector<double>& covered, double threshold) {
    int score = 0;
    for (std::size_t p = 0; p < scene.polygons.size(); ++p) {
        if (covered[p] + 1e-10 >= threshold * scene.polygons[p].perimeter) ++score;
    }
    return score;
}

struct CgalVerificationResult {
    bool feasible = true;
    int real_score = 0;
    std::size_t near_threshold = 0;
    std::vector<double> covered_by_polygon;
    std::vector<std::string> violations;
};

[[nodiscard]] VerifierFT verifier_segment_parameter(
    const VerifierSegment& source,
    const VerifierPoint& point)
{
    const auto direction = source.to_vector();
    const VerifierFT denominator = direction.squared_length();
    if (denominator == VerifierFT(0)) return VerifierFT(0);
    const VerifierFT numerator = (point - source.source()) * direction;
    return numerator / denominator;
}

[[nodiscard]] CgalVerificationResult verify_solution_with_cgal(
    const Scene& scene,
    const Box& domain_box,
    const std::vector<std::size_t>& selected_candidates,
    const std::vector<double>& cached_covered,
    const Options& options)
{
    const auto started = std::chrono::steady_clock::now();
    CgalVerificationResult result;
    result.covered_by_polygon.assign(scene.polygons.size(), 0.0);

    if (selected_candidates.size() != options.k) {
        result.feasible = false;
        result.violations.push_back(
            "selected cardinality is " + std::to_string(selected_candidates.size()) +
            ", expected " + std::to_string(options.k));
    }

    std::vector<std::uint8_t> selected_seen(scene.candidates.size(), 0);
    for (const std::size_t candidate : selected_candidates) {
        if (candidate >= scene.candidates.size()) {
            result.feasible = false;
            result.violations.push_back(
                "selected candidate index " + std::to_string(candidate) + " is out of range");
            continue;
        }
        if (selected_seen[candidate] != 0) {
            result.feasible = false;
            result.violations.push_back(
                "selected candidate index " + std::to_string(candidate) + " is duplicated");
        }
        selected_seen[candidate] = 1;
    }

    std::cout << "CGAL verifier: constructing an exact arrangement from "
              << scene.segments.size() << " source edges...\n" << std::flush;
    VerifierArrangement environment;
    const std::array<Point, 4> box_points{
        make_point(x(domain_box.min_corner()), y(domain_box.min_corner())),
        make_point(x(domain_box.max_corner()), y(domain_box.min_corner())),
        make_point(x(domain_box.max_corner()), y(domain_box.max_corner())),
        make_point(x(domain_box.min_corner()), y(domain_box.max_corner()))};
    std::vector<VerifierSegment> environment_segments;
    environment_segments.reserve(scene.segments.size() + box_points.size());
    for (std::size_t i = 0; i < box_points.size(); ++i) {
        environment_segments.emplace_back(
            verifier_point(box_points[i]),
            verifier_point(box_points[(i + 1) % box_points.size()]));
    }

    std::vector<VerifierSegment> exact_source_segments;
    exact_source_segments.reserve(scene.segments.size());
    std::vector<SegmentValue> source_boxes;
    source_boxes.reserve(scene.segments.size());
    double coordinate_scale = 1.0;
    for (std::uint32_t sid = 0; sid < scene.segments.size(); ++sid) {
        const SegmentData& segment = scene.segments[sid];
        exact_source_segments.emplace_back(
            verifier_point(segment.a), verifier_point(segment.b));
        source_boxes.emplace_back(segment_box(segment.a, segment.b), sid);
        coordinate_scale = std::max({coordinate_scale,
            std::abs(x(segment.a)), std::abs(y(segment.a)),
            std::abs(x(segment.b)), std::abs(y(segment.b))});
        environment_segments.push_back(exact_source_segments.back());
    }
    CGAL::insert(environment, environment_segments.begin(), environment_segments.end());
    const SegmentRTree source_tree(source_boxes.begin(), source_boxes.end());

    VerifierVisibility visibility(environment);
    VerifierPointLocation point_location(environment);

    using VerifierRing = std::vector<VerifierPoint>;
    std::vector<std::vector<VerifierRing>> exact_polygon_rings;
    exact_polygon_rings.reserve(scene.polygons.size());
    for (const PolygonData& polygon : scene.polygons) {
        std::vector<VerifierRing> rings;
        rings.reserve(polygon.rings.size());
        for (const RingData& ring : polygon.rings) {
            VerifierRing exact_ring;
            exact_ring.reserve(ring.vertices.size());
            for (const Point& point : ring.vertices) {
                exact_ring.push_back(verifier_point(point));
            }
            rings.push_back(std::move(exact_ring));
        }
        exact_polygon_rings.push_back(std::move(rings));
    }
    auto exact_point_in_any_obstacle = [&](const VerifierPoint& point) {
        for (const auto& rings : exact_polygon_rings) {
            if (rings.empty() || rings.front().size() < 3) continue;
            const CGAL::Bounded_side exterior_side = CGAL::bounded_side_2(
                rings.front().begin(), rings.front().end(), point, VerifierKernel());
            if (exterior_side != CGAL::ON_BOUNDED_SIDE) continue;

            bool inside_hole = false;
            for (std::size_t ring_id = 1; ring_id < rings.size(); ++ring_id) {
                if (rings[ring_id].size() < 3) continue;
                const CGAL::Bounded_side hole_side = CGAL::bounded_side_2(
                    rings[ring_id].begin(), rings[ring_id].end(), point, VerifierKernel());
                if (hole_side != CGAL::ON_UNBOUNDED_SIDE) {
                    inside_hole = true;
                    break;
                }
            }
            if (!inside_hole) return true;
        }
        return false;
    };

    using FaceConstHandle = VerifierArrangement::Face_const_handle;
    using HalfedgeConstHandle = VerifierArrangement::Halfedge_const_handle;
    using VertexConstHandle = VerifierArrangement::Vertex_const_handle;

    // Endpoints are retained in EPECK's exact number type through intersection
    // and interval union. Conversion to double happens only when multiplying by
    // the source edge length used by the original objective.
    std::vector<std::vector<std::pair<VerifierFT, VerifierFT>>> intervals_by_segment(
        scene.segments.size());
    const double broadphase_padding = 1e-9 * coordinate_scale;

    auto exact_face_for_probe = [&](
        const BoundaryPoint& sample,
        const VerifierPoint& exact_query) -> std::optional<FaceConstHandle>
    {
        std::vector<Point> probe_directions;
        probe_directions.reserve(4);
        auto add_direction = [&](Point direction) {
            const double length = std::hypot(x(direction), y(direction));
            if (length <= 1e-14) return;
            direction = make_point(x(direction) / length, y(direction) / length);
            for (const Point& existing : probe_directions) {
                if (std::abs(x(existing) - x(direction)) <= 1e-14 &&
                    std::abs(y(existing) - y(direction)) <= 1e-14) {
                    return;
                }
            }
            probe_directions.push_back(direction);
        };

        add_direction(sample.free_space_normal);
        std::vector<Point> incident_normals;
        incident_normals.reserve(2);
        for (const std::uint32_t sid : sample.incident_segments) {
            if (sid == std::numeric_limits<std::uint32_t>::max()) continue;
            const Point normal = free_space_unit_normal(scene, sid);
            const double length = std::hypot(x(normal), y(normal));
            if (length <= 1e-14) continue;
            incident_normals.push_back(
                make_point(x(normal) / length, y(normal) / length));
            add_direction(incident_normals.back());
        }
        // At a source vertex, either individual edge normal can lie exactly on
        // the adjacent edge.  Its probe is then located on an edge forever,
        // rather than inside the desired face.  The sum points into the open
        // free-space sector and is used only to identify the arrangement face;
        // it does not move or otherwise alter the guard.
        for (std::size_t i = 0; i < incident_normals.size(); ++i) {
            for (std::size_t j = i + 1; j < incident_normals.size(); ++j) {
                add_direction(make_point(
                    x(incident_normals[i]) + x(incident_normals[j]),
                    y(incident_normals[i]) + y(incident_normals[j])));
            }
        }
        if (probe_directions.empty()) return std::nullopt;

        const double initial_step = std::max(
            1e-9,
            128.0 * std::numeric_limits<double>::epsilon() * coordinate_scale);
        for (const Point& direction : probe_directions) {
            for (int attempt = 0; attempt < 16; ++attempt) {
                const double step = std::ldexp(initial_step, -attempt);
                const VerifierPoint probe(
                    exact_query.x() + VerifierFT(step * x(direction)),
                    exact_query.y() + VerifierFT(step * y(direction)));
                const auto probe_location = point_location.locate(probe);
                if (const auto* face =
                        cgal_variant_get_if<FaceConstHandle>(probe_location)) {
                    if (!exact_point_in_any_obstacle(probe)) return *face;
                }
            }
        }
        return std::nullopt;
    };

    auto add_visibility_edge = [&](const VerifierSegment& visible_edge) {
        if (visible_edge.is_degenerate()) return;
        const Point a(
            CGAL::to_double(visible_edge.source().x()),
            CGAL::to_double(visible_edge.source().y()));
        const Point b(
            CGAL::to_double(visible_edge.target().x()),
            CGAL::to_double(visible_edge.target().y()));
        const Box query(
            make_point(std::min(x(a), x(b)) - broadphase_padding,
                       std::min(y(a), y(b)) - broadphase_padding),
            make_point(std::max(x(a), x(b)) + broadphase_padding,
                       std::max(y(a), y(b)) + broadphase_padding));
        std::vector<SegmentValue> nearby;
        source_tree.query(bgi::intersects(query), std::back_inserter(nearby));
        for (const SegmentValue& value : nearby) {
            const std::uint32_t sid = value.second;
            const auto intersection = CGAL::intersection(
                visible_edge, exact_source_segments[sid]);
            if (!intersection) continue;
            const auto* overlap =
                cgal_variant_get_if<VerifierSegment>(*intersection);
            if (overlap == nullptr || overlap->is_degenerate()) continue;
            VerifierFT t0 = verifier_segment_parameter(
                exact_source_segments[sid], overlap->source());
            VerifierFT t1 = verifier_segment_parameter(
                exact_source_segments[sid], overlap->target());
            if (t0 > t1) std::swap(t0, t1);
            t0 = std::max(VerifierFT(0), t0);
            t1 = std::min(VerifierFT(1), t1);
            if (t1 > t0) intervals_by_segment[sid].emplace_back(t0, t1);
        }
    };

    std::size_t verified_guards = 0;
    for (std::size_t slot = 0; slot < selected_candidates.size(); ++slot) {
        const std::size_t candidate = selected_candidates[slot];
        if (candidate >= scene.candidates.size()) continue;
        const BoundaryPoint& sample =
            scene.samples.at(scene.candidates.at(candidate));
        VerifierPoint query = verifier_point(sample.point);
        if (sample.has_canonical_edge_parameter &&
            sample.canonical_edge_segment < exact_source_segments.size()) {
            const VerifierSegment& edge =
                exact_source_segments[sample.canonical_edge_segment];
            const VerifierFT t(sample.canonical_edge_parameter);
            query = VerifierPoint(
                edge.source().x() + t * (edge.target().x() - edge.source().x()),
                edge.source().y() + t * (edge.target().y() - edge.source().y()));
        }
        const auto location = point_location.locate(query);
        VerifierArrangement output;
        VerifierArrangement::Face_handle visible_face;
        bool query_ok = false;

        if (const auto* face = cgal_variant_get_if<FaceConstHandle>(location)) {
            if ((*face)->is_unbounded() || exact_point_in_any_obstacle(query)) {
                result.feasible = false;
                result.violations.push_back(
                    "guard slot " + std::to_string(slot + 1) +
                    " is not in free space");
            } else {
                visible_face = visibility.compute_visibility(query, *face, output);
                query_ok = true;
            }
        } else {
            const std::optional<FaceConstHandle> desired_face =
                exact_face_for_probe(sample, query);
            if (!desired_face.has_value() || (*desired_face)->is_unbounded()) {
                result.feasible = false;
                result.violations.push_back(
                    "guard slot " + std::to_string(slot + 1) +
                    " has no identifiable free-space side");
            } else if (const auto* edge =
                           cgal_variant_get_if<HalfedgeConstHandle>(location)) {
                HalfedgeConstHandle chosen = (*edge)->face() == *desired_face
                    ? *edge
                    : (*edge)->twin();
                if (chosen->face() != *desired_face) {
                    result.feasible = false;
                    result.violations.push_back(
                        "guard slot " + std::to_string(slot + 1) +
                        " lies on an edge that does not bound its free-space face");
                } else {
                    if (chosen->source()->point() == query) chosen = chosen->prev();
                    visible_face = visibility.compute_visibility(query, chosen, output);
                    query_ok = true;
                }
            } else if (const auto* vertex =
                           cgal_variant_get_if<VertexConstHandle>(location)) {
                if ((*vertex)->is_isolated()) {
                    result.feasible = false;
                    result.violations.push_back(
                        "guard slot " + std::to_string(slot + 1) +
                        " coincides with an isolated arrangement vertex");
                } else {
                    auto first = (*vertex)->incident_halfedges();
                    auto current = first;
                    HalfedgeConstHandle chosen;
                    do {
                        HalfedgeConstHandle incident = current;
                        if (incident->face() == *desired_face) {
                            chosen = incident;
                            break;
                        }
                        ++current;
                    } while (current != first);
                    if (chosen == HalfedgeConstHandle()) {
                        result.feasible = false;
                        result.violations.push_back(
                            "guard slot " + std::to_string(slot + 1) +
                            " vertex is not incident to the identified free-space face");
                    } else {
                        visible_face = visibility.compute_visibility(query, chosen, output);
                        query_ok = true;
                    }
                }
            } else {
                result.feasible = false;
                result.violations.push_back(
                    "guard slot " + std::to_string(slot + 1) +
                    " has an unknown arrangement location");
            }
        }

        if (!query_ok) continue;
        ++verified_guards;
        (void)visible_face;
        // Non-regularized visibility retains one-dimensional collinear
        // continuations (needles). Every output edge is therefore relevant,
        // including edges that are not on the returned face's outer CCB.
        for (auto edge = output.edges_begin(); edge != output.edges_end(); ++edge) {
            add_visibility_edge(VerifierSegment(
                edge->source()->point(), edge->target()->point()));
        }
        std::cout << "CGAL verifier: guard " << (slot + 1) << "/"
                  << selected_candidates.size() << " checked.\n";
    }

    if (verified_guards != selected_candidates.size()) {
        result.feasible = false;
    }

    for (std::size_t sid = 0; sid < scene.segments.size(); ++sid) {
        auto& intervals = intervals_by_segment[sid];
        if (intervals.empty()) continue;
        std::sort(intervals.begin(), intervals.end());
        VerifierFT covered_parameter(0);
        VerifierFT lo = intervals.front().first;
        VerifierFT hi = intervals.front().second;
        for (std::size_t i = 1; i < intervals.size(); ++i) {
            if (intervals[i].first <= hi) {
                hi = std::max(hi, intervals[i].second);
            } else {
                covered_parameter += hi - lo;
                lo = intervals[i].first;
                hi = intervals[i].second;
            }
        }
        covered_parameter += hi - lo;
        const SegmentData& segment = scene.segments[sid];
        const double covered_fraction = std::clamp(
            CGAL::to_double(covered_parameter), 0.0, 1.0);
        result.covered_by_polygon[segment.polygon_id] +=
            covered_fraction * distance(segment.a, segment.b);
    }

    const fs::path report_path = options.output_dir / "cgal_verification.csv";
    std::ofstream report(report_path, std::ios::trunc);
    if (!report) {
        throw std::runtime_error(
            "Could not write CGAL verification report: " + report_path.string());
    }
    report << "polygon_id,source_fid,perimeter_m,exact_visible_m,exact_fraction,"
              "threshold,exact_qualified,cached_visible_m,cached_fraction,"
              "cached_qualified,visible_delta_m,threshold_margin_m,near_threshold\n";
    report << std::setprecision(17);
    for (const PolygonData& polygon : scene.polygons) {
        const std::size_t pid = polygon.internal_id;
        const double exact_visible = result.covered_by_polygon[pid];
        const double exact_fraction = polygon.perimeter > 0.0
            ? exact_visible / polygon.perimeter
            : 0.0;
        const double cached_visible = pid < cached_covered.size()
            ? cached_covered[pid]
            : 0.0;
        const double cached_fraction = polygon.perimeter > 0.0
            ? cached_visible / polygon.perimeter
            : 0.0;
        const double target = options.threshold * polygon.perimeter;
        const double margin = exact_visible - target;
        const double ambiguity = 1e-9 * std::max(1.0, polygon.perimeter);
        const bool near = std::abs(margin) <= ambiguity;
        const bool exact_qualified = exact_visible >= target;
        const bool cached_qualified = cached_visible + 1e-10 >= target;
        if (exact_qualified) ++result.real_score;
        if (near) ++result.near_threshold;
        report << polygon.internal_id << ',' << polygon.source_fid << ','
               << polygon.perimeter << ',' << exact_visible << ','
               << exact_fraction << ',' << options.threshold << ','
               << (exact_qualified ? 1 : 0) << ',' << cached_visible << ','
               << cached_fraction << ',' << (cached_qualified ? 1 : 0) << ','
               << (exact_visible - cached_visible) << ',' << margin << ','
               << (near ? 1 : 0) << '\n';
    }

    const fs::path summary_path = options.output_dir / "cgal_verification.txt";
    std::ofstream summary(summary_path, std::ios::trunc);
    if (!summary) {
        throw std::runtime_error(
            "Could not write CGAL verification summary: " + summary_path.string());
    }
    const int cached_score = solution_score(scene, cached_covered, options.threshold);
    const double seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    summary << "feasible=" << (result.feasible ? "yes" : "no") << '\n'
            << "selected=" << selected_candidates.size() << '\n'
            << "expected_k=" << options.k << '\n'
            << "verified_guards=" << verified_guards << '\n'
            << "real_score=" << result.real_score << '\n'
            << "cached_score=" << cached_score << '\n'
            << "score_delta=" << (result.real_score - cached_score) << '\n'
            << "near_threshold_polygons=" << result.near_threshold << '\n'
            << "runtime_seconds=" << std::setprecision(17) << seconds << '\n';
    for (const std::string& violation : result.violations) {
        summary << "violation=" << violation << '\n';
    }

    std::cout << "CGAL verifier: "
              << (result.feasible ? "solution is feasible" : "SOLUTION IS INFEASIBLE")
              << "; real score=" << result.real_score << "/"
              << scene.polygons.size() << ", cached score=" << cached_score << "/"
              << scene.polygons.size() << ", delta="
              << (result.real_score - cached_score) << ".\n"
              << "CGAL verifier reports: " << report_path << " and "
              << summary_path << " (" << seconds << " s).\n";
    if (result.near_threshold > 0) {
        std::cout << "CGAL verifier warning: " << result.near_threshold
                  << " polygon(s) are within the floating length-reporting tolerance "
                     "of the threshold; non-regularized visibility topology itself "
                     "was computed with exact predicates and constructions.\n";
    }
    for (const std::string& violation : result.violations) {
        std::cerr << "CGAL verifier violation: " << violation << '\n';
    }
    return result;
}

[[nodiscard]] double sum_covered(const std::vector<double>& covered) {
    return std::accumulate(covered.begin(), covered.end(), 0.0);
}

class OptimizationTimeline {
public:
    OptimizationTimeline(const fs::path& path, const Scene& scene, double final_threshold,
                         std::size_t k, double curve_exponent)
        : scene_(scene), final_threshold_(final_threshold), k_(k),
          curve_exponent_(curve_exponent), start_(std::chrono::steady_clock::now())
    {
        if (path.has_parent_path()) fs::create_directories(path.parent_path());
        out_.open(path, std::ios::trunc);
        if (!out_) {
            throw std::runtime_error("Could not write optimization timeline: " + path.string());
        }
        out_ << "row,elapsed_seconds,event,phase,move_number,cardinality,k,"
                "real_threshold,effective_threshold,curve_exponent,"
                "real_score_fixed_t,effective_score_curve,covered_boundary_m,polygon_count,"
                "added_candidate,removed_candidate,polish_round\n";
        out_ << std::setprecision(17);
    }

    void record(std::string_view event, std::string_view phase, std::size_t move_number,
                std::size_t cardinality, const std::vector<double>& covered,
                std::size_t added_candidate = std::numeric_limits<std::size_t>::max(),
                std::size_t removed_candidate = std::numeric_limits<std::size_t>::max(),
                std::size_t polish_round = 0)
    {
        const double elapsed = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - start_).count();
        const double ratio = k_ > 0
            ? static_cast<double>(cardinality) / static_cast<double>(k_)
            : 0.0;
        const double effective_threshold = cardinality == 0
            ? 0.0
            : final_threshold_ * std::pow(ratio, curve_exponent_);
        const int real_score = solution_score(scene_, covered, final_threshold_);
        const int effective_score = cardinality == 0
            ? 0
            : solution_score(scene_, covered, effective_threshold);

        out_ << row_++ << ',' << elapsed << ',' << event << ',' << phase << ','
             << move_number << ',' << cardinality << ',' << k_ << ','
             << final_threshold_ << ',' << effective_threshold << ',' << curve_exponent_ << ','
             << real_score << ',' << effective_score << ',' << sum_covered(covered) << ','
             << scene_.polygons.size() << ',';
        if (added_candidate == std::numeric_limits<std::size_t>::max()) out_ << -1;
        else out_ << added_candidate;
        out_ << ',';
        if (removed_candidate == std::numeric_limits<std::size_t>::max()) out_ << -1;
        else out_ << removed_candidate;
        out_ << ',' << polish_round << '\n';
        out_.flush();
    }

private:
    const Scene& scene_;
    double final_threshold_ = 0.0;
    std::size_t k_ = 0;
    double curve_exponent_ = 1.0;
    std::chrono::steady_clock::time_point start_;
    std::ofstream out_;
    std::size_t row_ = 0;
};

struct SelectionState {
    std::vector<std::uint32_t> cover_count;
    std::vector<double> covered;
};

[[nodiscard]] SelectionState evaluate_selection(
    const Scene& scene,
    const VisibilityCache& cache,
    const std::vector<std::size_t>& selected)
{
    SelectionState state;
    state.cover_count.assign(scene.samples.size(), 0);
    state.covered.assign(scene.polygons.size(), 0.0);
    for (const std::size_t candidate : selected) {
        cache.for_each(candidate, [&](std::uint32_t sample_id) {
            if (state.cover_count[sample_id]++ == 0) {
                const BoundaryPoint& sample = scene.samples[sample_id];
                state.covered[sample.polygon_id] += sample.weight;
            }
        });
    }
    return state;
}

struct ReplacementNeighborhoodEvaluation {
    std::vector<int> best_score;
    std::vector<double> best_total;
    std::vector<std::size_t> best_slot;
    std::vector<int> addition_crossings;
    std::vector<double> rescue_gain;
    std::vector<double> novel_gain;
};

[[nodiscard]] ReplacementNeighborhoodEvaluation evaluate_replacement_neighborhood(
    const Scene& scene,
    const VisibilityCache& cache,
    const std::vector<std::size_t>& selected,
    const std::vector<std::uint8_t>& selected_flag,
    const std::vector<std::uint32_t>& cover_count,
    const std::vector<double>& covered,
    double threshold,
    const CandidatePolygonMap* candidate_polygon_map = nullptr,
    const std::vector<std::uint64_t>* polygon_changed_round = nullptr,
    const std::vector<std::uint64_t>* candidate_checked_round = nullptr,
    std::size_t forbidden_removal_slot = std::numeric_limits<std::size_t>::max(),
    const std::vector<std::size_t>* candidate_subset = nullptr,
    bool compact_subset_result = false)
{
    const std::size_t polygon_count = scene.polygons.size();
    const std::size_t candidate_count = scene.candidates.size();
    const std::size_t l = selected.size();
    const double current_total = sum_covered(covered);
    const std::size_t work_count = candidate_subset == nullptr
        ? candidate_count
        : candidate_subset->size();
    const bool compact_result =
        candidate_subset != nullptr && compact_subset_result;
    const std::size_t result_count = compact_result
        ? work_count
        : candidate_count;

    ReplacementNeighborhoodEvaluation result;
    result.best_score.assign(result_count, -1);
    result.best_total.assign(result_count, -1.0);
    result.best_slot.assign(result_count, l);
    result.addition_crossings.assign(result_count, -1);
    result.rescue_gain.assign(result_count, -1.0);
    result.novel_gain.assign(result_count, -1.0);
    if (l == 0) return result;

    std::vector<std::int32_t> unique_owner(scene.samples.size(), -1);
    std::vector<double> unique_loss(l * polygon_count, 0.0);
    std::vector<double> unique_total(l, 0.0);
    for (std::size_t slot = 0; slot < l; ++slot) {
        cache.for_each(selected[slot], [&](std::uint32_t sample_id) {
            if (cover_count[sample_id] != 1) return;
            unique_owner[sample_id] = static_cast<std::int32_t>(slot);
            const BoundaryPoint& sample = scene.samples[sample_id];
            unique_loss[slot * polygon_count + sample.polygon_id] += sample.weight;
            unique_total[slot] += sample.weight;
        });
    }

    std::vector<int> base_remove_score(l, 0);
    for (std::size_t slot = 0; slot < l; ++slot) {
        for (std::size_t p = 0; p < polygon_count; ++p) {
            if (covered[p] - unique_loss[slot * polygon_count + p] + 1e-10 >=
                threshold * scene.polygons[p].perimeter) {
                ++base_remove_score[slot];
            }
        }
    }

#ifdef GIS_CUP_HAS_OPENMP
    // When this evaluator is called from the parallel anchor loop, keep the
    // candidate loop single-threaded inside that worker. This avoids nested
    // OpenMP teams and oversubscription. Top-level calls still use all workers.
    const bool create_parallel_team = omp_in_parallel() == 0;
#pragma omp parallel if(create_parallel_team)
#endif
    {
        std::vector<double> common_gain(polygon_count, 0.0);
        std::vector<std::uint32_t> common_stamp(polygon_count, 0);
        std::vector<std::uint32_t> common_touched;
        std::vector<double> restore(l * polygon_count, 0.0);
        std::vector<std::uint32_t> restore_stamp(l * polygon_count, 0);
        std::vector<std::vector<std::uint32_t>> restore_touched(l);
        std::vector<double> restore_total(l, 0.0);
        std::uint32_t epoch = 1;
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp for schedule(dynamic, 32)
#endif
        for (std::int64_t work_signed = 0;
             work_signed < static_cast<std::int64_t>(work_count);
             ++work_signed) {
            const std::size_t work = static_cast<std::size_t>(work_signed);
            const std::size_t ci = candidate_subset == nullptr
                ? work
                : candidate_subset->at(work);
            const std::size_t result_index = compact_result ? work : ci;
            if (ci >= candidate_count || selected_flag[ci] ||
                !cache.is_search_candidate(ci)) continue;
            if (candidate_polygon_map != nullptr &&
                polygon_changed_round != nullptr &&
                candidate_checked_round != nullptr &&
                !candidate_is_hot(ci, *candidate_polygon_map,
                                  *polygon_changed_round,
                                  *candidate_checked_round)) {
                continue;
            }
            if (++epoch == 0) {
                std::fill(common_stamp.begin(), common_stamp.end(), 0);
                std::fill(restore_stamp.begin(), restore_stamp.end(), 0);
                epoch = 1;
            }
            common_touched.clear();
            for (auto& values : restore_touched) values.clear();
            std::fill(restore_total.begin(), restore_total.end(), 0.0);
            double common_total = 0.0;

            cache.for_each(ci, [&](std::uint32_t sample_id) {
                const BoundaryPoint& sample = scene.samples[sample_id];
                const std::size_t pid = sample.polygon_id;
                if (cover_count[sample_id] == 0) {
                    if (common_stamp[pid] != epoch) {
                        common_stamp[pid] = epoch;
                        common_gain[pid] = 0.0;
                        common_touched.push_back(static_cast<std::uint32_t>(pid));
                    }
                    common_gain[pid] += sample.weight;
                    common_total += sample.weight;
                } else if (cover_count[sample_id] == 1 && unique_owner[sample_id] >= 0) {
                    const std::size_t slot = static_cast<std::size_t>(unique_owner[sample_id]);
                    const std::size_t index = slot * polygon_count + pid;
                    if (restore_stamp[index] != epoch) {
                        restore_stamp[index] = epoch;
                        restore[index] = 0.0;
                        restore_touched[slot].push_back(static_cast<std::uint32_t>(pid));
                    }
                    restore[index] += sample.weight;
                    restore_total[slot] += sample.weight;
                }
            });

            int addition_crossings = 0;
            double rescue_gain = 0.0;
            for (const std::uint32_t pid : common_touched) {
                const double target = threshold * scene.polygons[pid].perimeter;
                if (covered[pid] + 1e-10 >= target) continue;
                const double deficit = std::max(0.0, target - covered[pid]);
                rescue_gain += std::min(deficit, common_gain[pid]);
                if (covered[pid] + common_gain[pid] + 1e-10 >= target) ++addition_crossings;
            }

            int best_score = -1;
            double best_total = -1.0;
            std::size_t best_slot = l;
            for (std::size_t slot = 0; slot < l; ++slot) {
                if (slot == forbidden_removal_slot) continue;
                int score = base_remove_score[slot];
                for (const std::uint32_t pid : common_touched) {
                    const double after_remove = covered[pid] - unique_loss[slot * polygon_count + pid];
                    const std::size_t index = slot * polygon_count + pid;
                    const double gain = common_gain[pid] +
                        (restore_stamp[index] == epoch ? restore[index] : 0.0);
                    if (after_remove + 1e-10 < threshold * scene.polygons[pid].perimeter &&
                        after_remove + gain + 1e-10 >= threshold * scene.polygons[pid].perimeter) {
                        ++score;
                    }
                }
                for (const std::uint32_t pid : restore_touched[slot]) {
                    if (common_stamp[pid] == epoch) continue;
                    const double after_remove = covered[pid] - unique_loss[slot * polygon_count + pid];
                    const double gain = restore[slot * polygon_count + pid];
                    if (after_remove + 1e-10 < threshold * scene.polygons[pid].perimeter &&
                        after_remove + gain + 1e-10 >= threshold * scene.polygons[pid].perimeter) {
                        ++score;
                    }
                }
                const double total = current_total - unique_total[slot] + common_total + restore_total[slot];
                if (best_slot == l || score > best_score ||
                    (score == best_score && total > best_total + 1e-12) ||
                    (score == best_score && std::abs(total - best_total) <= 1e-12 && slot < best_slot)) {
                    best_score = score;
                    best_total = total;
                    best_slot = slot;
                }
            }

            result.best_score[result_index] = best_score;
            result.best_total[result_index] = best_total;
            result.best_slot[result_index] = best_slot;
            result.addition_crossings[result_index] = addition_crossings;
            result.rescue_gain[result_index] = rescue_gain;
            result.novel_gain[result_index] = common_total;
        }
    }
    return result;
}

[[nodiscard]] std::vector<std::size_t> build_polish_pool(
    const Scene& scene,
    const VisibilityCache& cache,
    const std::vector<std::size_t>& selected,
    const std::vector<std::uint32_t>& current_cover_count,
    const std::vector<double>& current_covered,
    std::size_t target_cardinality,
    double threshold,
    std::size_t requested_extra,
    bool verbose,
    std::string_view pool_label,
    const ReplacementNeighborhoodEvaluation* precomputed_evaluation = nullptr)
{
    const std::size_t candidate_count = scene.candidates.size();
    if (selected.size() != target_cardinality) {
        throw std::runtime_error("Pool incumbent cardinality does not match target cardinality");
    }
    requested_extra = std::min(requested_extra, candidate_count - selected.size());

    std::vector<std::size_t> pool = selected;
    pool.reserve(selected.size() + requested_extra);
    if (requested_extra == 0 || selected.empty()) return pool;

    std::vector<std::uint8_t> selected_flag(candidate_count, 0);
    for (const std::size_t candidate : selected) selected_flag[candidate] = 1;
    std::optional<ReplacementNeighborhoodEvaluation> computed_evaluation;
    if (precomputed_evaluation == nullptr) {
        computed_evaluation.emplace(evaluate_replacement_neighborhood(
            scene, cache, selected, selected_flag,
            current_cover_count, current_covered, threshold));
        precomputed_evaluation = &*computed_evaluation;
    }
    const ReplacementNeighborhoodEvaluation& evaluation = *precomputed_evaluation;

    std::vector<std::size_t> candidates;
    candidates.reserve(cache.search_candidate_count());
    for (std::size_t ci = 0; ci < candidate_count; ++ci) {
        if (!selected_flag[ci] && cache.is_search_candidate(ci) &&
            evaluation.best_slot[ci] < selected.size()) {
            candidates.push_back(ci);
        }
    }
    if (candidates.empty()) return pool;

    std::vector<std::uint8_t> chosen(candidate_count, 0);
    for (const std::size_t candidate : selected) chosen[candidate] = 1;
    std::vector<std::size_t> target_use(selected.size(), 0);

    const std::size_t replacement_quota = std::min(
        requested_extra, (requested_extra * 3 + 4) / 5); // ceil(60%).
    const std::size_t rescue_quota = std::min(
        requested_extra - replacement_quota, requested_extra / 5); // 20%.
    const std::size_t novelty_quota = requested_extra - replacement_quota - rescue_quota;

    auto replacement_better = [&](std::size_t a, std::size_t b) {
        if (evaluation.best_score[a] != evaluation.best_score[b]) {
            return evaluation.best_score[a] > evaluation.best_score[b];
        }
        if (std::abs(evaluation.best_total[a] - evaluation.best_total[b]) > 1e-12) {
            return evaluation.best_total[a] > evaluation.best_total[b];
        }
        if (evaluation.addition_crossings[a] != evaluation.addition_crossings[b]) {
            return evaluation.addition_crossings[a] > evaluation.addition_crossings[b];
        }
        if (std::abs(evaluation.rescue_gain[a] - evaluation.rescue_gain[b]) > 1e-12) {
            return evaluation.rescue_gain[a] > evaluation.rescue_gain[b];
        }
        if (std::abs(evaluation.novel_gain[a] - evaluation.novel_gain[b]) > 1e-12) {
            return evaluation.novel_gain[a] > evaluation.novel_gain[b];
        }
        return a < b;
    };
    auto rescue_better = [&](std::size_t a, std::size_t b) {
        if (evaluation.addition_crossings[a] != evaluation.addition_crossings[b]) {
            return evaluation.addition_crossings[a] > evaluation.addition_crossings[b];
        }
        if (std::abs(evaluation.rescue_gain[a] - evaluation.rescue_gain[b]) > 1e-12) {
            return evaluation.rescue_gain[a] > evaluation.rescue_gain[b];
        }
        return replacement_better(a, b);
    };
    auto novelty_better = [&](std::size_t a, std::size_t b) {
        if (std::abs(evaluation.novel_gain[a] - evaluation.novel_gain[b]) > 1e-12) {
            return evaluation.novel_gain[a] > evaluation.novel_gain[b];
        }
        return rescue_better(a, b);
    };

    auto append_candidate = [&](std::size_t candidate, std::string_view category) {
        chosen[candidate] = 1;
        pool.push_back(candidate);
        const std::size_t slot = evaluation.best_slot[candidate];
        if (slot < target_use.size()) ++target_use[slot];
        if (verbose) {
            std::cout << pool_label << " pool extension " << (pool.size() - selected.size()) << "/" << requested_extra
                      << " [" << category << "]: candidate=" << candidate
                      << ", best_replace_slot=" << (slot + 1)
                      << ", best_replace_candidate=" << selected[slot]
                      << ", replacement_score=" << evaluation.best_score[candidate]
                      << ", replacement_covered=" << evaluation.best_total[candidate]
                      << ", addition_crossings=" << evaluation.addition_crossings[candidate]
                      << ", rescue_gain=" << evaluation.rescue_gain[candidate]
                      << ", novel_gain=" << evaluation.novel_gain[candidate] << " m\n";
        }
    };

    std::sort(candidates.begin(), candidates.end(), replacement_better);
    std::size_t replacement_added = 0;
    for (const std::size_t target_cap : {std::size_t{1}, std::size_t{2},
                                         std::numeric_limits<std::size_t>::max()}) {
        for (const std::size_t candidate : candidates) {
            if (replacement_added >= replacement_quota) break;
            if (chosen[candidate]) continue;
            const std::size_t slot = evaluation.best_slot[candidate];
            if (slot >= target_use.size() || target_use[slot] >= target_cap) continue;
            append_candidate(candidate, "replacement");
            ++replacement_added;
        }
        if (replacement_added >= replacement_quota) break;
    }

    std::sort(candidates.begin(), candidates.end(), rescue_better);
    std::size_t rescue_added = 0;
    for (const std::size_t candidate : candidates) {
        if (rescue_added >= rescue_quota) break;
        if (chosen[candidate]) continue;
        append_candidate(candidate, "threshold_rescue");
        ++rescue_added;
    }

    std::sort(candidates.begin(), candidates.end(), novelty_better);
    std::size_t novelty_added = 0;
    for (const std::size_t candidate : candidates) {
        if (novelty_added >= novelty_quota) break;
        if (chosen[candidate]) continue;
        append_candidate(candidate, "novelty");
        ++novelty_added;
    }

    // Fill any shortfall deterministically with the strongest remaining replacement candidates.
    std::sort(candidates.begin(), candidates.end(), replacement_better);
    for (const std::size_t candidate : candidates) {
        if (pool.size() >= selected.size() + requested_extra) break;
        if (chosen[candidate]) continue;
        append_candidate(candidate, "fallback");
    }
    return pool;
}

[[nodiscard]] std::vector<std::size_t> build_multi_swap_anchor_pool(
    const Scene& scene,
    const VisibilityCache& cache,
    const Options& options,
    const CandidatePolygonMap& candidate_polygon_map,
    const CandidatePolygonReverseIndex& polygon_reverse_index,
    const std::vector<std::size_t>& selected,
    const std::vector<std::uint32_t>& current_cover_count,
    const std::vector<double>& current_covered,
    double threshold,
    bool verbose)
{
    const std::size_t candidate_count = scene.candidates.size();
    std::vector<std::uint8_t> selected_flag(candidate_count, 0);
    for (const std::size_t candidate : selected) selected_flag[candidate] = 1;

    const ReplacementNeighborhoodEvaluation base_evaluation =
        evaluate_replacement_neighborhood(
            scene, cache, selected, selected_flag,
            current_cover_count, current_covered, threshold);

    std::vector<std::size_t> pool = build_polish_pool(
        scene, cache, selected, current_cover_count, current_covered,
        selected.size(), threshold, options.multi_swap_extra_count,
        verbose, "restricted multi-swap anchor", &base_evaluation);

    const std::size_t anchor_end = pool.size();
    const std::size_t anchor_count = anchor_end - selected.size();
    if (anchor_count == 0 || options.multi_swap_partners_per_anchor == 0) {
        return pool;
    }

    const std::size_t maximum_partner_additions = std::min(
        candidate_count - pool.size(),
        anchor_count * options.multi_swap_partners_per_anchor);
    pool.reserve(pool.size() + maximum_partner_additions);

    // Every anchor is ranked independently against the same immutable base
    // pool. Results are committed later, in anchor order, so duplicate partner
    // choices are removed deterministically without any shared writes in the
    // expensive parallel section.
    std::vector<std::uint8_t> base_chosen(candidate_count, 0);
    for (const std::size_t candidate : pool) base_chosen[candidate] = 1;

    const int incumbent_score = solution_score(scene, current_covered, threshold);
    const double incumbent_total = sum_covered(current_covered);

    struct PartnerRank {
        std::size_t candidate = 0;
        std::size_t removed_candidate = std::numeric_limits<std::size_t>::max();
        bool improves_incumbent = false;
        int synergy_score = std::numeric_limits<int>::min();
        double synergy_total = -std::numeric_limits<double>::infinity();
        int pair_score = -1;
        double pair_total = -1.0;
        int standalone_score = -1;
    };

    struct AnchorPartnerResult {
        bool valid = false;
        std::size_t anchor = 0;
        std::size_t removed_incumbent = std::numeric_limits<std::size_t>::max();
        std::size_t sampled_polygon_count = 0;
        std::size_t sampled_candidate_count = 0;
        double elapsed_seconds = 0.0;
        std::vector<PartnerRank> ranked;
    };

    auto partner_better = [](const PartnerRank& a, const PartnerRank& b) {
        if (a.improves_incumbent != b.improves_incumbent) {
            return a.improves_incumbent > b.improves_incumbent;
        }
        if (a.synergy_score != b.synergy_score) {
            return a.synergy_score > b.synergy_score;
        }
        if (std::abs(a.synergy_total - b.synergy_total) > 1e-12) {
            return a.synergy_total > b.synergy_total;
        }
        if (a.pair_score != b.pair_score) return a.pair_score > b.pair_score;
        if (std::abs(a.pair_total - b.pair_total) > 1e-12) {
            return a.pair_total > b.pair_total;
        }
        if (a.standalone_score != b.standalone_score) {
            return a.standalone_score < b.standalone_score;
        }
        return a.candidate < b.candidate;
    };

    std::size_t anchor_workers = 1;
#ifdef GIS_CUP_HAS_OPENMP
    anchor_workers = std::min<std::size_t>(
        anchor_count, static_cast<std::size_t>(omp_get_max_threads()));
#endif
    if (verbose) {
        std::cout << "Restricted multi-swap pair-aware pool expansion: anchors="
                  << anchor_count << ", partners_per_anchor="
                  << options.multi_swap_partners_per_anchor
                  << ", polygon_samples="
                  << options.multi_swap_anchor_polygon_samples
                  << ", candidate_samples="
                  << options.multi_swap_anchor_candidate_samples
                  << ", base_pool=" << anchor_end
                  << ", maximum_pool="
                  << (anchor_end + maximum_partner_additions)
                  << ", anchor_workers=" << anchor_workers
                  << ". Anchor relationships only guide pool construction; "
                     "CP-SAT receives one flat pool.\n";
    }

    const auto expansion_start = std::chrono::steady_clock::now();
    std::vector<AnchorPartnerResult> anchor_results(anchor_count);
    std::atomic<std::size_t> anchors_completed{0};
    std::atomic<bool> anchor_failed{false};
    std::exception_ptr anchor_exception;
    const std::size_t progress_step = std::max<std::size_t>(1, anchor_count / 10);

#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel
#endif
    {
        // Reused by every anchor handled by this worker. This avoids allocating
        // and clearing a candidate_count-sized deduplication bitmap per anchor.
        std::vector<std::uint32_t> candidate_stamp(candidate_count, 0);
        std::uint32_t candidate_epoch = 1;

#ifdef GIS_CUP_HAS_OPENMP
#pragma omp for schedule(dynamic, 1)
#endif
        for (std::int64_t anchor_offset_signed = 0;
             anchor_offset_signed < static_cast<std::int64_t>(anchor_count);
             ++anchor_offset_signed) {
            if (anchor_failed.load(std::memory_order_relaxed)) continue;
            const std::size_t anchor_offset =
                static_cast<std::size_t>(anchor_offset_signed);
            const std::size_t anchor_index = selected.size() + anchor_offset;

            try {
                const auto anchor_start = std::chrono::steady_clock::now();
                AnchorPartnerResult result;
                result.anchor = pool[anchor_index];
                const std::size_t anchor = result.anchor;
                const std::size_t replaced_slot =
                    base_evaluation.best_slot[anchor];
                if (replaced_slot < selected.size()) {
                    std::vector<std::size_t> provisional_selected = selected;
                    result.removed_incumbent =
                        provisional_selected[replaced_slot];
                    provisional_selected[replaced_slot] = anchor;

                    SelectionState provisional_state{
                        current_cover_count, current_covered};
                    cache.for_each(result.removed_incumbent,
                        [&](std::uint32_t sample_id) {
                            std::uint32_t& count =
                                provisional_state.cover_count[sample_id];
                            if (count == 0) {
                                throw std::runtime_error(
                                    "Anchor pool construction found an invalid incumbent cover count");
                            }
                            --count;
                            if (count == 0) {
                                const BoundaryPoint& sample =
                                    scene.samples[sample_id];
                                provisional_state.covered[sample.polygon_id] -=
                                    sample.weight;
                            }
                        });
                    cache.for_each(anchor, [&](std::uint32_t sample_id) {
                        std::uint32_t& count =
                            provisional_state.cover_count[sample_id];
                        if (count++ == 0) {
                            const BoundaryPoint& sample = scene.samples[sample_id];
                            provisional_state.covered[sample.polygon_id] +=
                                sample.weight;
                        }
                    });

                    std::vector<std::uint8_t> provisional_selected_flag =
                        selected_flag;
                    provisional_selected_flag[result.removed_incumbent] = 0;
                    provisional_selected_flag[anchor] = 1;

                    std::vector<std::uint32_t> anchor_polygons =
                        candidate_polygon_map.polygons_by_candidate.at(anchor);
                    if (!anchor_polygons.empty()) {
                        std::mt19937_64 rng(
                            options.multi_swap_anchor_seed ^
                            (static_cast<std::uint64_t>(anchor) *
                             0x9E3779B97F4A7C15ULL) ^
                            (static_cast<std::uint64_t>(anchor_index) *
                             0xD6E8FEB86659FD93ULL));
                        std::shuffle(anchor_polygons.begin(),
                                     anchor_polygons.end(), rng);
                        if (options.multi_swap_anchor_polygon_samples > 0 &&
                            anchor_polygons.size() >
                                options.multi_swap_anchor_polygon_samples) {
                            anchor_polygons.resize(
                                options.multi_swap_anchor_polygon_samples);
                        }
                        result.sampled_polygon_count = anchor_polygons.size();

                        if (++candidate_epoch == 0) {
                            std::fill(candidate_stamp.begin(),
                                      candidate_stamp.end(), 0);
                            candidate_epoch = 1;
                        }
                        const std::uint32_t this_epoch = candidate_epoch;
                        const std::size_t candidate_limit =
                            options.multi_swap_anchor_candidate_samples == 0
                                ? candidate_count
                                : std::min(
                                      options.multi_swap_anchor_candidate_samples,
                                      candidate_count);
                        std::vector<std::size_t> partner_candidates;
                        partner_candidates.reserve(std::min(
                            candidate_limit, cache.search_candidate_count()));

                        auto try_add_candidate = [&](std::size_t candidate) {
                            if (partner_candidates.size() >= candidate_limit) return;
                            if (candidate >= candidate_count ||
                                candidate_stamp[candidate] == this_epoch) return;
                            candidate_stamp[candidate] = this_epoch;
                            if (base_chosen[candidate] ||
                                provisional_selected_flag[candidate] ||
                                !cache.is_search_candidate(candidate)) return;
                            partner_candidates.push_back(candidate);
                        };

                        const std::size_t per_polygon_target =
                            std::max<std::size_t>(
                                1, (candidate_limit + anchor_polygons.size() - 1) /
                                       anchor_polygons.size());
                        for (const std::uint32_t polygon_id : anchor_polygons) {
                            if (partner_candidates.size() >= candidate_limit) break;
                            const auto& reverse =
                                polygon_reverse_index.candidates_by_polygon.at(
                                    polygon_id);
                            if (reverse.empty()) continue;

                            if (reverse.size() <= per_polygon_target * 2) {
                                for (const std::uint32_t candidate : reverse) {
                                    try_add_candidate(candidate);
                                    if (partner_candidates.size() >=
                                        candidate_limit) break;
                                }
                            } else {
                                std::uniform_int_distribution<std::size_t> pick(
                                    0, reverse.size() - 1);
                                const std::size_t attempts =
                                    std::min<std::size_t>(
                                        reverse.size(),
                                        per_polygon_target * 8 + 16);
                                const std::size_t before =
                                    partner_candidates.size();
                                for (std::size_t attempt = 0;
                                     attempt < attempts &&
                                     partner_candidates.size() < candidate_limit &&
                                     partner_candidates.size() - before <
                                         per_polygon_target;
                                     ++attempt) {
                                    try_add_candidate(reverse[pick(rng)]);
                                }
                            }
                        }
                        result.sampled_candidate_count =
                            partner_candidates.size();

                        if (!partner_candidates.empty()) {
                            // This evaluator detects that it is already inside
                            // an OpenMP region and therefore stays single-threaded
                            // for this anchor. Parallelism is across anchors,
                            // avoiding nested teams and oversubscription.
                            const ReplacementNeighborhoodEvaluation conditional =
                                evaluate_replacement_neighborhood(
                                    scene, cache, provisional_selected,
                                    provisional_selected_flag,
                                    provisional_state.cover_count,
                                    provisional_state.covered, threshold,
                                    nullptr, nullptr, nullptr, replaced_slot,
                                    &partner_candidates, true);

                            const int anchor_score = solution_score(
                                scene, provisional_state.covered, threshold);
                            const double anchor_total =
                                sum_covered(provisional_state.covered);
                            result.ranked.reserve(partner_candidates.size());
                            for (std::size_t partner_index = 0;
                                 partner_index < partner_candidates.size();
                                 ++partner_index) {
                                const std::size_t candidate =
                                    partner_candidates[partner_index];
                                const std::size_t partner_slot =
                                    conditional.best_slot[partner_index];
                                if (base_chosen[candidate] ||
                                    partner_slot >= selected.size()) continue;
                                const int pair_score =
                                    conditional.best_score[partner_index];
                                if (pair_score < 0) continue;
                                const double pair_total =
                                    conditional.best_total[partner_index];
                                const int standalone_score =
                                    base_evaluation.best_score[candidate];
                                const double standalone_total =
                                    base_evaluation.best_total[candidate];

                                const int individual_score = std::max(
                                    anchor_score, standalone_score);
                                double individual_total =
                                    -std::numeric_limits<double>::infinity();
                                if (anchor_score == individual_score) {
                                    individual_total = std::max(
                                        individual_total, anchor_total);
                                }
                                if (standalone_score == individual_score) {
                                    individual_total = std::max(
                                        individual_total, standalone_total);
                                }
                                const double synergy_total =
                                    std::isfinite(individual_total)
                                        ? pair_total - individual_total
                                        : pair_total;
                                const bool improves_incumbent =
                                    pair_score > incumbent_score ||
                                    (pair_score == incumbent_score &&
                                     pair_total > incumbent_total +
                                         options.swap_improvement_epsilon);

                                result.ranked.push_back({
                                    candidate,
                                    provisional_selected[partner_slot],
                                    improves_incumbent,
                                    pair_score - individual_score,
                                    synergy_total,
                                    pair_score,
                                    pair_total,
                                    standalone_score});
                            }
                            std::sort(result.ranked.begin(),
                                      result.ranked.end(), partner_better);
                        }
                        result.valid = true;
                    }
                }
                result.elapsed_seconds = std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - anchor_start).count();
                anchor_results[anchor_offset] = std::move(result);
            } catch (...) {
                anchor_failed.store(true, std::memory_order_relaxed);
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp critical(gis_cup_anchor_exception)
#endif
                {
                    if (!anchor_exception) {
                        anchor_exception = std::current_exception();
                    }
                }
            }

            const std::size_t done =
                anchors_completed.fetch_add(1, std::memory_order_relaxed) + 1;
            if (verbose &&
                (done == anchor_count || done % progress_step == 0)) {
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp critical(gis_cup_anchor_progress)
#endif
                {
                    std::cout << "Restricted multi-swap conditional ranking: "
                              << done << "/" << anchor_count
                              << " anchors complete.\n";
                }
            }
        }
    }

    if (anchor_exception) std::rethrow_exception(anchor_exception);

    const double expansion_seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - expansion_start).count();
    if (verbose) {
        double summed_anchor_seconds = 0.0;
        for (const auto& result : anchor_results) {
            summed_anchor_seconds += result.elapsed_seconds;
        }
        std::cout << "Restricted multi-swap conditional rankings generated in "
                  << expansion_seconds << " s wall time ("
                  << summed_anchor_seconds
                  << " aggregate anchor-seconds).\n";
    }

    // Commit in the original anchor order. If two anchors ranked the same
    // partner, the earlier anchor receives it and the later anchor falls back
    // to its next-best unique candidate. This makes the parallel result stable.
    std::vector<std::uint8_t> chosen = base_chosen;
    std::size_t partners_added = 0;
    for (const AnchorPartnerResult& result : anchor_results) {
        if (!result.valid || partners_added >= maximum_partner_additions) continue;
        std::size_t accepted_for_anchor = 0;
        for (const PartnerRank& partner : result.ranked) {
            if (accepted_for_anchor >=
                    options.multi_swap_partners_per_anchor ||
                partners_added >= maximum_partner_additions) {
                break;
            }
            if (chosen[partner.candidate]) continue;
            chosen[partner.candidate] = 1;
            pool.push_back(partner.candidate);
            ++partners_added;
            ++accepted_for_anchor;
            if (verbose) {
                std::cout << "Restricted multi-swap conditional partner: anchor="
                          << result.anchor
                          << ", sampled_polygons="
                          << result.sampled_polygon_count
                          << ", sampled_candidates="
                          << result.sampled_candidate_count
                          << ", anchor_removed="
                          << result.removed_incumbent
                          << ", partner=" << partner.candidate
                          << ", partner_removed="
                          << partner.removed_candidate
                          << ", pair_score=" << partner.pair_score
                          << ", pair_covered=" << partner.pair_total
                          << ", synergy_score=" << partner.synergy_score
                          << ", synergy_covered=" << partner.synergy_total
                          << ", improves_incumbent="
                          << (partner.improves_incumbent ? "yes" : "no")
                          << "\n";
            }
        }
    }

    if (verbose) {
        std::cout << "Restricted multi-swap expanded raw pool complete: incumbents="
                  << selected.size() << ", anchors=" << anchor_count
                  << ", conditional_partners=" << partners_added
                  << ", total_pool=" << pool.size()
                  << ". CP-SAT may select at most "
                  << options.multi_swap_max_new << " extras.\n";
    }
    return pool;
}

struct PoolMembership {
    std::uint32_t sample_id = 0;
    std::uint32_t pool_index = 0;
};

struct SignatureDigest {
    std::uint32_t polygon_id = 0;
    std::uint64_t hash1 = 0;
    std::uint64_t hash2 = 0;
    bool operator==(const SignatureDigest&) const = default;
};

struct SignatureDigestHash {
    std::size_t operator()(const SignatureDigest& key) const noexcept {
        std::uint64_t h = key.hash1 ^ (key.hash2 + 0x9E3779B97F4A7C15ULL + (key.hash1 << 6U) + (key.hash1 >> 2U));
        h ^= static_cast<std::uint64_t>(key.polygon_id) * 0xC2B2AE3D27D4EB4FULL;
        return static_cast<std::size_t>(h);
    }
};

struct VulnerableCoverageGroup {
    std::uint32_t polygon_id = 0;
    double weight = 0.0;
    std::vector<std::uint32_t> viewers;
};

struct PoolCoverageModelData {
    std::vector<VulnerableCoverageGroup> groups;
    std::vector<double> pool_visible_weight;
    std::size_t membership_count = 0;
    std::size_t guaranteed_sample_count = 0;
    std::size_t vulnerable_sample_count = 0;
};

[[nodiscard]] SignatureDigest signature_digest(
    std::uint32_t polygon_id,
    const std::vector<std::uint32_t>& viewers)
{
    std::uint64_t h1 = 1469598103934665603ULL;
    std::uint64_t h2 = 0x9E3779B97F4A7C15ULL;
    for (const std::uint32_t viewer : viewers) {
        h1 ^= viewer;
        h1 *= 1099511628211ULL;
        h2 ^= static_cast<std::uint64_t>(viewer) + 0x9E3779B97F4A7C15ULL + (h2 << 6U) + (h2 >> 2U);
        h2 *= 0xD6E8FEB86659FD93ULL;
    }
    return {polygon_id, h1, h2};
}

[[nodiscard]] PoolCoverageModelData build_pool_coverage_model(
    const Scene& scene,
    const VisibilityCache& cache,
    const std::vector<std::size_t>& pool,
    std::size_t removal_budget,
    std::size_t incumbent_size,
    std::size_t max_incumbent_removals,
    bool verbose)
{
    if (pool.size() > static_cast<std::size_t>(std::numeric_limits<std::uint32_t>::max())) {
        throw std::runtime_error("Pool is too large for 32-bit pool indices");
    }

    std::vector<PoolMembership> memberships;
    memberships.reserve(pool.size() * 1024);
    for (std::size_t pool_index = 0; pool_index < pool.size(); ++pool_index) {
        cache.for_each(pool[pool_index], [&](std::uint32_t sample_id) {
            memberships.push_back({sample_id, static_cast<std::uint32_t>(pool_index)});
        });
    }
    std::sort(memberships.begin(), memberships.end(), [](const PoolMembership& a, const PoolMembership& b) {
        if (a.sample_id != b.sample_id) return a.sample_id < b.sample_id;
        return a.pool_index < b.pool_index;
    });

    PoolCoverageModelData result;
    result.pool_visible_weight.assign(scene.polygons.size(), 0.0);
    result.membership_count = memberships.size();
    std::unordered_multimap<SignatureDigest, std::size_t, SignatureDigestHash> lookup;
    lookup.reserve(memberships.size() / 16 + 1);

    std::vector<std::uint32_t> viewers;
    viewers.reserve(removal_budget + 1);
    std::size_t pos = 0;
    while (pos < memberships.size()) {
        const std::uint32_t sample_id = memberships[pos].sample_id;
        viewers.clear();
        while (pos < memberships.size() && memberships[pos].sample_id == sample_id) {
            const std::uint32_t viewer = memberships[pos].pool_index;
            if (viewers.empty() || viewers.back() != viewer) viewers.push_back(viewer);
            ++pos;
        }

        const BoundaryPoint& sample = scene.samples[sample_id];
        result.pool_visible_weight[sample.polygon_id] += sample.weight;

        // Exactly removal_budget pool points are discarded. In a restricted
        // exchange, at most max_incumbent_removals incumbent points can leave.
        // Either condition below proves that at least one viewer survives, so
        // the sample is constant covered contribution and needs no SAT variable.
        const std::size_t incumbent_viewers = static_cast<std::size_t>(
            std::count_if(viewers.begin(), viewers.end(),
                          [&](std::uint32_t viewer) {
                              return static_cast<std::size_t>(viewer) < incumbent_size;
                          }));
        if (viewers.size() > removal_budget ||
            incumbent_viewers > max_incumbent_removals) {
            ++result.guaranteed_sample_count;
            continue;
        }

        ++result.vulnerable_sample_count;
        const SignatureDigest digest = signature_digest(sample.polygon_id, viewers);
        std::size_t group_index = result.groups.size();
        const auto range = lookup.equal_range(digest);
        for (auto it = range.first; it != range.second; ++it) {
            if (result.groups[it->second].viewers == viewers) {
                group_index = it->second;
                break;
            }
        }
        if (group_index == result.groups.size()) {
            result.groups.push_back({sample.polygon_id, sample.weight, viewers});
            lookup.emplace(digest, group_index);
        } else {
            result.groups[group_index].weight += sample.weight;
        }
    }

    if (verbose) {
        std::cout << "Pool model aggregation: " << result.membership_count
                  << " pool-sample memberships; " << result.guaranteed_sample_count
                  << " samples guaranteed covered, " << result.vulnerable_sample_count
                  << " vulnerable samples -> " << result.groups.size()
                  << " vulnerable viewer-signature groups.\n";
    }
    return result;
}

struct PoolSolveResult {
    std::vector<std::size_t> selected_candidates;
    bool optimal = false;
    std::string status;
    double solver_objective = 0.0;
};

[[nodiscard]] std::string cp_sat_status_name(operations_research::sat::CpSolverStatus status) {
    using operations_research::sat::CpSolverStatus;
    switch (status) {
        case CpSolverStatus::UNKNOWN: return "UNKNOWN";
        case CpSolverStatus::MODEL_INVALID: return "MODEL_INVALID";
        case CpSolverStatus::FEASIBLE: return "FEASIBLE";
        case CpSolverStatus::INFEASIBLE: return "INFEASIBLE";
        case CpSolverStatus::OPTIMAL: return "OPTIMAL";
    }
    return "UNRECOGNIZED";
}

[[nodiscard]] std::int64_t scaled_nonnegative(double value, std::int64_t scale, const char* what) {
    if (!(value >= 0.0) || !std::isfinite(value)) {
        throw std::runtime_error(std::string("Invalid nonnegative value while scaling ") + what);
    }
    const long double scaled = static_cast<long double>(value) * static_cast<long double>(scale);
    if (scaled > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        throw std::runtime_error(std::string("CP-SAT coefficient overflow while scaling ") + what);
    }
    if (value > 0.0 && scaled < 0.5L) return 1;
    return static_cast<std::int64_t>(std::llround(scaled));
}

[[nodiscard]] std::int64_t scaled_threshold(double value, std::int64_t scale, const char* what) {
    if (!(value >= 0.0) || !std::isfinite(value)) {
        throw std::runtime_error(std::string("Invalid nonnegative threshold while scaling ") + what);
    }
    const long double scaled = static_cast<long double>(value) * static_cast<long double>(scale);
    if (scaled > static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        throw std::runtime_error(std::string("CP-SAT coefficient overflow while scaling ") + what);
    }
    // Qualification means covered >= target. Rounding the target upward avoids
    // declaring a polygon qualified merely because fixed-point conversion rounded
    // the target down. The final candidate solution is still rechecked in doubles.
    return static_cast<std::int64_t>(std::ceil(scaled - 1e-12L));
}

[[nodiscard]] PoolSolveResult solve_pool_cp_sat(
    const Scene& scene,
    const VisibilityCache& cache,
    const Options& options,
    const std::vector<std::size_t>& pool,
    const std::vector<std::size_t>& incumbent,
    std::size_t target_cardinality,
    double threshold,
    std::size_t max_new_selected,
    double time_limit,
    std::string_view solve_label,
    bool verbose)
{
    namespace sat = operations_research::sat;

    if (incumbent.size() != target_cardinality) {
        throw std::runtime_error("CP-SAT incumbent cardinality does not match target cardinality");
    }
    if (pool.size() <= target_cardinality) {
        return {incumbent, true, "pool has no extra candidates", 0.0};
    }
    for (std::size_t j = 0; j < incumbent.size(); ++j) {
        if (pool[j] != incumbent[j]) {
            throw std::runtime_error("Pool must begin with the incumbent in incumbent order");
        }
    }

    // Coverage is monotone, so selecting at most target_cardinality points has an
    // optimum with exactly target_cardinality points. Model the small complement.
    const std::size_t removal_budget = pool.size() - target_cardinality;
    const std::size_t effective_max_new = std::min(
        max_new_selected, pool.size() - incumbent.size());
    const PoolCoverageModelData coverage = build_pool_coverage_model(
        scene, cache, pool, removal_budget, incumbent.size(),
        effective_max_new, verbose);

    const std::size_t polygon_count = scene.polygons.size();
    std::vector<std::vector<std::size_t>> groups_by_polygon(polygon_count);
    double vulnerable_weight_total = 0.0;
    double max_pool_visible_weight = 0.0;
    for (std::size_t group_index = 0; group_index < coverage.groups.size(); ++group_index) {
        groups_by_polygon[coverage.groups[group_index].polygon_id].push_back(group_index);
        vulnerable_weight_total += coverage.groups[group_index].weight;
    }
    for (const double value : coverage.pool_visible_weight) {
        max_pool_visible_weight = std::max(max_pool_visible_weight, value);
    }

    // CP-SAT requires integer coefficients. Use micrometre-scale weights when
    // safe, and reduce the scale automatically if the lexicographic objective
    // could approach int64 limits. The independently reconstructed double score
    // remains the acceptance authority after the solve.
    constexpr std::int64_t desired_scale = 1'000'000;
    const long double safe_limit = static_cast<long double>(std::numeric_limits<std::int64_t>::max()) / 16.0L;
    long double max_scale = static_cast<long double>(desired_scale);
    if (vulnerable_weight_total > 0.0 && polygon_count > 0) {
        max_scale = std::min(max_scale,
            safe_limit /
            (static_cast<long double>(polygon_count + 1) * static_cast<long double>(vulnerable_weight_total)));
    }
    if (max_pool_visible_weight > 0.0) {
        max_scale = std::min(max_scale,
            safe_limit / static_cast<long double>(max_pool_visible_weight));
    }
    const std::int64_t weight_scale = std::max<std::int64_t>(1, static_cast<std::int64_t>(std::floor(max_scale)));

    if (max_scale < 1.0L) {
        throw std::runtime_error("Pool weights are too large for a safe CP-SAT fixed-point model");
    }

    std::vector<std::int64_t> group_weight(coverage.groups.size(), 0);
    std::vector<std::int64_t> vulnerable_weight_by_polygon(polygon_count, 0);
    std::vector<double> vulnerable_double_by_polygon(polygon_count, 0.0);
    std::vector<std::int64_t> pool_visible_weight(polygon_count, 0);
    std::vector<std::int64_t> target_weight(polygon_count, 0);
    std::int64_t vulnerable_weight_upper = 0;
    for (std::size_t g = 0; g < coverage.groups.size(); ++g) {
        group_weight[g] = scaled_nonnegative(coverage.groups[g].weight, weight_scale, "group weight");
        if (group_weight[g] > std::numeric_limits<std::int64_t>::max() - vulnerable_weight_upper) {
            throw std::runtime_error("CP-SAT vulnerable-weight sum overflow");
        }
        vulnerable_weight_upper += group_weight[g];
        const std::uint32_t polygon_id = coverage.groups[g].polygon_id;
        if (group_weight[g] > std::numeric_limits<std::int64_t>::max() - vulnerable_weight_by_polygon[polygon_id]) {
            throw std::runtime_error("CP-SAT polygon vulnerable-weight sum overflow");
        }
        vulnerable_weight_by_polygon[polygon_id] += group_weight[g];
        vulnerable_double_by_polygon[polygon_id] += coverage.groups[g].weight;
    }
    for (std::size_t polygon_id = 0; polygon_id < polygon_count; ++polygon_id) {
        // Build the full pool-visible integer weight from the exact sum of the
        // vulnerable integer groups plus a separately scaled guaranteed-covered
        // constant. This keeps the integer model internally consistent despite
        // per-group rounding.
        const double guaranteed_double = std::max(
            0.0, coverage.pool_visible_weight[polygon_id] - vulnerable_double_by_polygon[polygon_id]);
        const std::int64_t guaranteed_weight = scaled_nonnegative(
            guaranteed_double, weight_scale, "guaranteed pool-visible weight");
        if (guaranteed_weight > std::numeric_limits<std::int64_t>::max() - vulnerable_weight_by_polygon[polygon_id]) {
            throw std::runtime_error("CP-SAT pool-visible weight overflow");
        }
        pool_visible_weight[polygon_id] =
            guaranteed_weight + vulnerable_weight_by_polygon[polygon_id];
        target_weight[polygon_id] = scaled_threshold(
            threshold * scene.polygons[polygon_id].perimeter,
            weight_scale,
            "polygon target");
    }

    sat::CpModelBuilder cp_model;
    cp_model.SetName(std::string(solve_label));

    std::vector<sat::BoolVar> removed;
    removed.reserve(pool.size());
    for (std::size_t j = 0; j < pool.size(); ++j) {
        removed.push_back(cp_model.NewBoolVar().WithName("remove_" + std::to_string(j)));
    }
    sat::LinearExpr removal_sum;
    for (const sat::BoolVar var : removed) removal_sum += var;
    cp_model.AddEquality(removal_sum, static_cast<std::int64_t>(removal_budget));

    // The pool begins with the incumbent and ends with temporary candidates.
    // Because cardinality is fixed, the number of selected temporary candidates
    // exactly equals the number of removed incumbent candidates. Limiting the
    // latter therefore enforces an at-most-effective_max_new exchange.
    if (effective_max_new < incumbent.size()) {
        sat::LinearExpr removed_incumbent_sum;
        for (std::size_t j = 0; j < incumbent.size(); ++j) {
            removed_incumbent_sum += removed[j];
        }
        cp_model.AddLessOrEqual(
            removed_incumbent_sum,
            static_cast<std::int64_t>(effective_max_new));
    }

    // lost[g] is true exactly when all pool points that see group g are removed.
    // Encode this with native Boolean clauses. Singleton groups alias the
    // corresponding removal variable and need no auxiliary variable at all.
    std::vector<sat::BoolVar> lost;
    std::vector<std::uint8_t> lost_is_auxiliary;
    lost.reserve(coverage.groups.size());
    lost_is_auxiliary.reserve(coverage.groups.size());
    std::size_t lost_auxiliary_count = 0;
    for (std::size_t g = 0; g < coverage.groups.size(); ++g) {
        const VulnerableCoverageGroup& group = coverage.groups[g];
        if (group.viewers.empty()) throw std::runtime_error("Vulnerable group has no viewers");
        if (group.viewers.size() == 1) {
            lost.push_back(removed.at(group.viewers.front()));
            lost_is_auxiliary.push_back(0);
            continue;
        }

        const sat::BoolVar lost_var = cp_model.NewBoolVar().WithName("lost_" + std::to_string(g));
        lost.push_back(lost_var);
        lost_is_auxiliary.push_back(1);
        ++lost_auxiliary_count;

        // lost -> every viewer is removed.
        for (const std::uint32_t viewer : group.viewers) {
            cp_model.AddImplication(lost_var, removed.at(viewer));
        }
        // all viewers removed -> lost. Equivalently:
        // (not r_1) OR ... OR (not r_n) OR lost.
        std::vector<sat::BoolVar> clause;
        clause.reserve(group.viewers.size() + 1);
        for (const std::uint32_t viewer : group.viewers) {
            clause.push_back(removed.at(viewer).Not());
        }
        clause.push_back(lost_var);
        cp_model.AddBoolOr(clause);
    }

    std::vector<sat::BoolVar> qualified;
    std::vector<std::uint32_t> qualified_polygon;
    qualified.reserve(polygon_count);
    qualified_polygon.reserve(polygon_count);
    std::size_t always_qualified = 0;
    for (std::size_t polygon_id = 0; polygon_id < polygon_count; ++polygon_id) {
        if (target_weight[polygon_id] <= 0) {
            ++always_qualified;
            continue;
        }
        if (pool_visible_weight[polygon_id] < target_weight[polygon_id]) continue;
        // Even deleting every vulnerable group on this polygon would leave it
        // above threshold, so it is qualified under every feasible removal set.
        if (pool_visible_weight[polygon_id] - vulnerable_weight_by_polygon[polygon_id] >=
            target_weight[polygon_id]) {
            ++always_qualified;
            continue;
        }

        const sat::BoolVar z = cp_model.NewBoolVar().WithName("qualified_" + std::to_string(polygon_id));
        qualified.push_back(z);
        qualified_polygon.push_back(static_cast<std::uint32_t>(polygon_id));

        sat::LinearExpr lost_on_polygon;
        for (const std::size_t group_index : groups_by_polygon[polygon_id]) {
            lost_on_polygon += sat::LinearExpr::Term(lost[group_index], group_weight[group_index]);
        }
        lost_on_polygon += sat::LinearExpr::Term(z, target_weight[polygon_id]);
        cp_model.AddLessOrEqual(lost_on_polygon, pool_visible_weight[polygon_id]);
    }

    // Exact lexicographic objective in integer arithmetic:
    //   1. maximize the number of qualified polygons;
    //   2. among ties, minimize lost covered boundary weight.
    // One qualification is worth more than the largest possible total loss.
    const std::int64_t qualification_multiplier = vulnerable_weight_upper + 1;
    if (!qualified.empty() &&
        qualification_multiplier > std::numeric_limits<std::int64_t>::max() /
            static_cast<std::int64_t>(qualified.size() + 1)) {
        throw std::runtime_error("CP-SAT lexicographic objective overflow");
    }
    sat::LinearExpr objective;
    for (const sat::BoolVar z : qualified) {
        objective += sat::LinearExpr::Term(z, qualification_multiplier);
    }
    for (std::size_t g = 0; g < lost.size(); ++g) {
        objective -= sat::LinearExpr::Term(lost[g], group_weight[g]);
    }
    cp_model.Maximize(objective);

    // Pool construction places the incumbent first and temporary additions last.
    // Supply a complete incumbent hint, including derived loss/qualification vars.
    for (std::size_t j = 0; j < removed.size(); ++j) {
        cp_model.AddHint(removed[j], j >= incumbent.size());
    }
    std::vector<std::uint8_t> hinted_lost(lost.size(), 0);
    for (std::size_t g = 0; g < coverage.groups.size(); ++g) {
        bool all_removed = true;
        for (const std::uint32_t viewer : coverage.groups[g].viewers) {
            if (viewer < incumbent.size()) {
                all_removed = false;
                break;
            }
        }
        hinted_lost[g] = static_cast<std::uint8_t>(all_removed ? 1 : 0);
        if (lost_is_auxiliary[g] != 0) cp_model.AddHint(lost[g], all_removed);
    }
    for (std::size_t q = 0; q < qualified.size(); ++q) {
        const std::uint32_t polygon_id = qualified_polygon[q];
        std::int64_t hinted_loss = 0;
        for (const std::size_t group_index : groups_by_polygon[polygon_id]) {
            if (hinted_lost[group_index] != 0) hinted_loss += group_weight[group_index];
        }
        cp_model.AddHint(
            qualified[q],
            pool_visible_weight[polygon_id] - hinted_loss >= target_weight[polygon_id]);
    }

    sat::SatParameters parameters;
    const std::size_t requested_workers = options.polish_workers > 0
        ? options.polish_workers
        : options.threads;
    if (requested_workers > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())) {
        throw std::runtime_error("--polish-workers/--threads is too large for CP-SAT");
    }
    parameters.set_num_workers(static_cast<std::int32_t>(requested_workers));
    if (time_limit > 0.0) {
        parameters.set_max_time_in_seconds(time_limit);
    }
    parameters.set_relative_gap_limit(options.polish_mip_gap);
    parameters.set_log_search_progress(options.polish_solver_log);
    parameters.set_log_to_stdout(true);

    sat::Model model;
    model.Add(sat::NewSatParameters(parameters));
    const auto solve_start = std::chrono::steady_clock::now();
    const sat::CpSolverResponse response = sat::SolveCpModel(cp_model.Build(), &model);
    const double solve_seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - solve_start).count();

    const sat::CpSolverStatus solver_status = response.status();
    const bool has_solution = solver_status == sat::CpSolverStatus::OPTIMAL ||
                              solver_status == sat::CpSolverStatus::FEASIBLE;
    const bool optimal = solver_status == sat::CpSolverStatus::OPTIMAL;
    const std::string status = cp_sat_status_name(solver_status);
    if (!has_solution) {
        if (verbose) {
            std::cout << solve_label << " CP-SAT solve returned no feasible solution: status="
                      << status << ", solve=" << solve_seconds << " s. Keeping incumbent.\n";
        }
        return {incumbent, false, status, response.objective_value()};
    }

    std::vector<std::size_t> selected;
    selected.reserve(target_cardinality);
    for (std::size_t j = 0; j < pool.size(); ++j) {
        if (!sat::SolutionBooleanValue(response, removed[j])) selected.push_back(pool[j]);
    }
    if (selected.size() != target_cardinality) {
        throw std::runtime_error(
            "CP-SAT returned " + std::to_string(selected.size()) +
            " selected pool points, expected " + std::to_string(target_cardinality));
    }

    std::size_t solver_qualified = always_qualified;
    for (const sat::BoolVar z : qualified) {
        if (sat::SolutionBooleanValue(response, z)) ++solver_qualified;
    }

    if (verbose) {
        std::cout << solve_label << " CP-SAT pool model: " << pool.size()
                  << " binary removal variables (remove " << removal_budget << "), max-new="
                  << effective_max_new << ", "
                  << coverage.groups.size() << " vulnerable groups ("
                  << lost_auxiliary_count << " auxiliary loss variables), "
                  << qualified.size() << " modeled qualification variables, "
                  << cp_model.Proto().constraints_size() << " constraints, scale="
                  << weight_scale << "; workers="
                  << (requested_workers == 0 ? std::string("all") : std::to_string(requested_workers))
                  << ", status=" << status
                  << ", modeled-qualified=" << solver_qualified << "/" << polygon_count
                  << ", objective=" << response.objective_value()
                  << ", bound=" << response.best_objective_bound()
                  << ", solve=" << solve_seconds << " s.\n";
    }
    return {std::move(selected), optimal, status, response.objective_value()};
}

struct GreedyResult {
    std::vector<std::size_t> selected_candidates;
    std::vector<double> covered_weight;
};

[[nodiscard]] GreedyResult local_search_select(
    const Scene& scene,
    const VisibilityCache& cache,
    const CandidatePolygonMap& candidate_polygon_map,
    const CandidatePolygonReverseIndex& candidate_polygon_reverse_index,
    const VisibilityEngine& engine,
    const Options& options,
    double curve_exponent,
    bool write_snapshots,
    bool verbose)
{
    const std::size_t polygon_count = scene.polygons.size();
    const std::size_t candidate_count = scene.candidates.size();
    const std::size_t search_candidate_count = cache.search_candidate_count();
    std::vector<std::uint32_t> cover_count(scene.samples.size(), 0);
    std::vector<std::uint8_t> selected_flag(candidate_count, 0);
    std::vector<double> covered(polygon_count, 0.0);
    std::vector<std::size_t> selected;
    selected.reserve(options.k);
    if (candidate_polygon_map.polygons_by_candidate.size() != candidate_count) {
        throw std::runtime_error("Candidate-visible-polygon map size mismatch");
    }
    // candidate_checked_round records the last solution state in which a
    // candidate was fully evaluated and rejected. polygon_changed_round records
    // the newest solution state that changed coverage on each polygon.
    //
    // A candidate is cold exactly when every polygon it sees has an epoch no
    // newer than its own last-check epoch.  This gives lazy invalidation without
    // traversing a reverse polygon-to-candidate index after a swap.
    std::vector<std::uint64_t> candidate_checked_round(candidate_count, 0);
    std::vector<std::uint64_t> polygon_changed_round(polygon_count, 0);
    std::uint64_t swap_state_round = 0;
    std::size_t move_number = 0;
    std::size_t last_snapshot_move = 0;
    std::optional<MoveInfo> last_move;
    std::optional<OptimizationTimeline> timeline;
    if (write_snapshots && !options.timeline_path.empty()) {
        timeline.emplace(options.timeline_path, scene, options.threshold, options.k, curve_exponent);
        timeline->record("phase_start", "greedy_to_k", 0, 0, covered);
        if (verbose) {
            std::cout << "Optimization timeline: " << options.timeline_path << "\n";
        }
    }

    auto apply_add = [&](std::size_t candidate) {
        cache.for_each(candidate, [&](std::uint32_t sample_id) {
            if (cover_count[sample_id]++ == 0) {
                const auto& sample = scene.samples[sample_id];
                covered[sample.polygon_id] += sample.weight;
            }
        });
        selected_flag[candidate] = 1;
    };
    auto apply_remove = [&](std::size_t candidate) {
        cache.for_each(candidate, [&](std::uint32_t sample_id) {
            if (cover_count[sample_id] == 0) throw std::runtime_error("Internal cover-count underflow");
            if (--cover_count[sample_id] == 0) {
                const auto& sample = scene.samples[sample_id];
                covered[sample.polygon_id] -= sample.weight;
            }
        });
        selected_flag[candidate] = 0;
    };
    auto emit_snapshot = [&](const MoveInfo& move, bool force) {
        if (!write_snapshots) return;
        if (!force) {
            if (options.snapshot_every == 0 || move.move_number % options.snapshot_every != 0) return;
        }
        if (last_snapshot_move == move.move_number) return;
        std::vector<std::vector<Point>> visibility_polygons(selected.size());
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel for schedule(dynamic, 1)
#endif
        for (std::int64_t slot_signed = 0; slot_signed < static_cast<std::int64_t>(selected.size()); ++slot_signed) {
            const std::size_t slot = static_cast<std::size_t>(slot_signed);
            visibility_polygons[slot] = engine.compute_visibility_polygon(selected[slot]);
        }
        write_step_snapshot(scene, engine, options.output_dir, move, selected, visibility_polygons, covered,
                            options.threshold, options.k, curve_exponent);
        last_snapshot_move = move.move_number;
    };

    auto run_swap_descent = [&](double threshold, std::string_view move_type) -> std::size_t {
        const std::string_view timeline_phase = move_type == "post_pool_swap"
            ? std::string_view("post_k_1swap")
            : std::string_view("greedy_to_k");
        if (timeline) {
            timeline->record("phase_start", timeline_phase, move_number, selected.size(), covered);
        }
        auto advance_state_round = [&]() {
            if (swap_state_round == std::numeric_limits<std::uint64_t>::max()) {
                // Practically unreachable, but keep the epoch comparison valid.
                std::fill(candidate_checked_round.begin(), candidate_checked_round.end(), 0);
                std::fill(polygon_changed_round.begin(), polygon_changed_round.end(), 0);
                swap_state_round = 1;
            } else {
                ++swap_state_round;
            }
        };

        // A new descent may use a different cardinality, threshold, or incumbent
        // (for example after CP-SAT), so invalidate every polygon once.  This is
        // O(number of polygons), not O(number of candidates).
        advance_state_round();
        std::fill(polygon_changed_round.begin(), polygon_changed_round.end(),
                  swap_state_round);

        std::size_t accepted_swaps = 0;
        while (!selected.empty() &&
               (options.max_swap_passes == 0 || accepted_swaps < options.max_swap_passes)) {
            const std::size_t l = selected.size();
            const int current_score = solution_score(scene, covered, threshold);
            const double current_total = sum_covered(covered);
            const ReplacementNeighborhoodEvaluation evaluation = evaluate_replacement_neighborhood(
                scene, cache, selected, selected_flag, cover_count, covered, threshold,
                &candidate_polygon_map, &polygon_changed_round,
                &candidate_checked_round);

            std::vector<std::size_t> evaluated_candidates;
            std::size_t best_in = candidate_count;
            std::size_t best_slot = l;
            int best_score = current_score;
            double best_total = current_total;
            for (std::size_t ci = 0; ci < candidate_count; ++ci) {
                if (evaluation.best_score[ci] < 0) continue;
                evaluated_candidates.push_back(ci);
                const int score = evaluation.best_score[ci];
                const double total = evaluation.best_total[ci];
                const bool improves = score > current_score ||
                    (score == current_score &&
                     total > current_total + options.swap_improvement_epsilon);
                if (!improves) continue;
                if (best_in == candidate_count || score > best_score ||
                    (score == best_score && total > best_total + 1e-12) ||
                    (score == best_score && std::abs(total - best_total) <= 1e-12 &&
                     ci < best_in)) {
                    best_in = ci;
                    best_slot = evaluation.best_slot[ci];
                    best_score = score;
                    best_total = total;
                }
            }

            // Every evaluated insertion candidate that was not accepted is now
            // certified cold for the current polygon-change epochs.
            for (const std::size_t ci : evaluated_candidates) {
                if (ci != best_in) candidate_checked_round[ci] = swap_state_round;
            }

            if (best_in == candidate_count) {
                const std::size_t replacements = evaluated_candidates.size() * l;
                const std::size_t available = search_candidate_count >= l
                    ? search_candidate_count - l
                    : 0;
                const std::size_t cold_skipped = available >= evaluated_candidates.size()
                    ? available - evaluated_candidates.size()
                    : 0;
                if (timeline) {
                    timeline->record("round_end_no_improvement", timeline_phase,
                                     move_number, selected.size(), covered);
                }
                if (verbose) {
                    std::cout << "Swap local optimum at cardinality=" << l
                              << " after evaluating " << replacements
                              << " replacements from " << evaluated_candidates.size()
                              << " hot candidates; skipped " << cold_skipped
                              << " cold candidates";
                    if (!move_type.empty()) std::cout << " [" << move_type << "]";
                    std::cout << ".\n";
                }
                break;
            }

            const std::size_t best_out = selected[best_slot];
            apply_remove(best_out);
            apply_add(best_in);
            selected[best_slot] = best_in;

            // Coverage can only have changed on polygons visible from the point
            // removed or the point inserted.  Give those polygons a fresh epoch;
            // candidates lazily discover that they are hot when next scanned.
            advance_state_round();
            std::size_t affected_polygon_count = 0;
            auto mark_changed_polygons = [&](std::size_t candidate) {
                for (const std::uint32_t polygon_id :
                     candidate_polygon_map.polygons_by_candidate[candidate]) {
                    if (polygon_changed_round[polygon_id] != swap_state_round) {
                        polygon_changed_round[polygon_id] = swap_state_round;
                        ++affected_polygon_count;
                    }
                }
            };
            mark_changed_polygons(best_out);
            mark_changed_polygons(best_in);

            ++accepted_swaps;
            ++move_number;
            MoveInfo swap_move{move_number, std::string(move_type), selected.size(), best_in,
                               best_out, best_slot,
                               solution_score(scene, covered, threshold),
                               sum_covered(covered)};
            last_move = swap_move;
            if (verbose) {
                std::cout << "Move " << move_number << " " << move_type
                          << " out=" << best_out << " in=" << best_in
                          << " slot=" << (best_slot + 1)
                          << " cardinality=" << selected.size()
                          << " score=" << swap_move.score << "/" << polygon_count
                          << ", checked_hot=" << evaluated_candidates.size()
                          << ", changed_polygons=" << affected_polygon_count
                          << ", state_round=" << swap_state_round << "\n";
            }
            emit_snapshot(swap_move, false);
            if (timeline) {
                timeline->record("accepted_move", timeline_phase, swap_move.move_number,
                                 swap_move.cardinality, covered, best_in, best_out);
            }
        }
        return accepted_swaps;
    };

    for (std::size_t target_cardinality = 1; target_cardinality <= options.k; ++target_cardinality) {
        if (timeline) {
            timeline->record("phase_start", "greedy_to_k", move_number, selected.size(), covered);
        }
        const double ratio = static_cast<double>(target_cardinality) / static_cast<double>(options.k);
        const double threshold = options.threshold * std::pow(ratio, curve_exponent);
        const int base_add_score = solution_score(scene, covered, threshold);
        const double base_add_total = sum_covered(covered);
        std::vector<int> add_scores(candidate_count, -1);
        std::vector<double> add_totals(candidate_count, -1.0);

#ifdef GIS_CUP_HAS_OPENMP
#pragma omp parallel
#endif
        {
            std::vector<double> delta(polygon_count, 0.0);
            std::vector<std::uint32_t> stamps(polygon_count, 0);
            std::vector<std::uint32_t> touched;
            std::uint32_t epoch = 1;
#ifdef GIS_CUP_HAS_OPENMP
#pragma omp for schedule(dynamic, 64)
#endif
            for (std::int64_t ci_signed = 0; ci_signed < static_cast<std::int64_t>(candidate_count); ++ci_signed) {
                const std::size_t ci = static_cast<std::size_t>(ci_signed);
                if (selected_flag[ci] || !cache.is_search_candidate(ci)) continue;
                if (++epoch == 0) { std::fill(stamps.begin(), stamps.end(), 0); epoch = 1; }
                touched.clear(); double gain_total = 0.0;
                cache.for_each(ci, [&](std::uint32_t sample_id) {
                    if (cover_count[sample_id] != 0) return;
                    const auto& sample = scene.samples[sample_id]; const std::uint32_t pid = sample.polygon_id;
                    if (stamps[pid] != epoch) { stamps[pid] = epoch; delta[pid] = 0.0; touched.push_back(pid); }
                    delta[pid] += sample.weight; gain_total += sample.weight;
                });
                int score = base_add_score;
                for (const std::uint32_t pid : touched) {
                    if (covered[pid] + 1e-10 < threshold * scene.polygons[pid].perimeter &&
                        covered[pid] + delta[pid] + 1e-10 >= threshold * scene.polygons[pid].perimeter) ++score;
                }
                add_scores[ci] = score; add_totals[ci] = base_add_total + gain_total;
            }
        }

        std::size_t best_add = candidate_count;
        for (std::size_t ci = 0; ci < candidate_count; ++ci) {
            if (add_scores[ci] < 0) continue;
            if (best_add == candidate_count || add_scores[ci] > add_scores[best_add] ||
                (add_scores[ci] == add_scores[best_add] && add_totals[ci] > add_totals[best_add] + 1e-12) ||
                (add_scores[ci] == add_scores[best_add] && std::abs(add_totals[ci] - add_totals[best_add]) <= 1e-12 && ci < best_add)) {
                best_add = ci;
            }
        }
        if (best_add == candidate_count) break;
        apply_add(best_add);
        selected.push_back(best_add);
        ++move_number;
        MoveInfo add_move{move_number, "add", selected.size(), best_add, std::numeric_limits<std::size_t>::max(), selected.size() - 1,
                          solution_score(scene, covered, threshold), sum_covered(covered)};
        last_move = add_move;
        if (verbose) {
            std::cout << "Move " << move_number << " add candidate=" << best_add
                      << " cardinality=" << selected.size() << " score=" << add_move.score << "/" << polygon_count << "\n";
        }
        emit_snapshot(add_move, false);
        if (timeline) {
            timeline->record("accepted_move", "greedy_to_k", add_move.move_number,
                             add_move.cardinality, covered, best_add);
        }

        // Always finish the hot-candidate 1-swap descent.  The more expensive
        // restricted CP-SAT neighborhood is scheduled only every Bth add round,
        // may introduce at most A newcomers, and may run for at most N attempts
        // at this cardinality.  An accepted restricted move returns to 1-swap.
        const bool multi_swap_scheduled =
            options.multi_swap_polish &&
            selected.size() > options.multi_swap_max_new &&
            (target_cardinality % options.multi_swap_every == 0);
        std::size_t multi_swap_round = 0;
        while (true) {
            (void)run_swap_descent(threshold, "swap");

            if (!multi_swap_scheduled) {
                if (verbose && options.multi_swap_polish &&
                    selected.size() > options.multi_swap_max_new &&
                    target_cardinality % options.multi_swap_every != 0) {
                    std::cout << "Restricted up-to-" << options.multi_swap_max_new
                              << "-swap CP-SAT skipped at add round "
                              << target_cardinality << ": scheduled every "
                              << options.multi_swap_every << " rounds.\n";
                }
                break;
            }
            if (options.multi_swap_max_rounds != 0 &&
                multi_swap_round >= options.multi_swap_max_rounds) {
                if (verbose) {
                    std::cout << "Restricted up-to-" << options.multi_swap_max_new
                              << "-swap CP-SAT reached --multi-swap-max-rounds="
                              << options.multi_swap_max_rounds
                              << " at add round " << target_cardinality
                              << "; advancing to the next add.\n";
                }
                break;
            }
            ++multi_swap_round;

            const int current_score = solution_score(scene, covered, threshold);
            const double current_total = sum_covered(covered);
            if (verbose) {
                std::cout << "Restricted up-to-" << options.multi_swap_max_new
                          << "-swap CP-SAT round " << multi_swap_round
                          << " at cardinality=" << selected.size()
                          << ": incumbent score=" << current_score << "/" << polygon_count
                          << ", covered=" << current_total << " m.\n";
            }

            if (timeline) {
                timeline->record("phase_start", "greedy_multi_swap_pool", move_number,
                                 selected.size(), covered,
                                 std::numeric_limits<std::size_t>::max(),
                                 std::numeric_limits<std::size_t>::max(),
                                 multi_swap_round);
            }
            const std::vector<std::size_t> pool = build_multi_swap_anchor_pool(
                scene, cache, options, candidate_polygon_map,
                candidate_polygon_reverse_index, selected, cover_count,
                covered, threshold, verbose);
            if (pool.size() <= selected.size()) {
                if (verbose) {
                    std::cout << "Restricted up-to-" << options.multi_swap_max_new
                              << "-swap CP-SAT skipped: no temporary candidate could be added.\n";
                }
                break;
            }

            if (timeline) {
                timeline->record("phase_start", "greedy_multi_swap", move_number,
                                 selected.size(), covered,
                                 std::numeric_limits<std::size_t>::max(),
                                 std::numeric_limits<std::size_t>::max(),
                                 multi_swap_round);
            }
            const std::string solve_label =
                "up-to-" + std::to_string(options.multi_swap_max_new) + "-swap";
            PoolSolveResult pool_result = solve_pool_cp_sat(
                scene, cache, options, pool, selected, selected.size(), threshold,
                options.multi_swap_max_new, options.multi_swap_time_limit,
                solve_label, verbose);
            SelectionState candidate_state = evaluate_selection(
                scene, cache, pool_result.selected_candidates);
            const int candidate_score = solution_score(
                scene, candidate_state.covered, threshold);
            const double candidate_total = sum_covered(candidate_state.covered);
            const bool improves = candidate_score > current_score ||
                (candidate_score == current_score &&
                 candidate_total > current_total + options.swap_improvement_epsilon);

            if (!improves) {
                if (timeline) {
                    timeline->record("round_end_no_improvement", "greedy_multi_swap",
                                     move_number, selected.size(), covered,
                                     std::numeric_limits<std::size_t>::max(),
                                     std::numeric_limits<std::size_t>::max(),
                                     multi_swap_round);
                }
                if (verbose) {
                    std::cout << "Restricted up-to-" << options.multi_swap_max_new
                              << "-swap CP-SAT found no strict improvement (status="
                              << pool_result.status
                              << "). Advancing to the next add round.\n";
                }
                break;
            }

            std::size_t newcomers = 0;
            for (const std::size_t candidate : pool_result.selected_candidates) {
                if (!selected_flag[candidate]) ++newcomers;
            }
            if (newcomers > options.multi_swap_max_new) {
                throw std::runtime_error(
                    "Restricted CP-SAT selected more newcomers than --multi-swap-max-new");
            }

            selected = std::move(pool_result.selected_candidates);
            cover_count = std::move(candidate_state.cover_count);
            covered = std::move(candidate_state.covered);
            std::fill(selected_flag.begin(), selected_flag.end(), 0);
            for (const std::size_t candidate : selected) selected_flag[candidate] = 1;

            ++move_number;
            MoveInfo pool_move{move_number, "pool_multi_swap", selected.size(),
                               std::numeric_limits<std::size_t>::max(),
                               std::numeric_limits<std::size_t>::max(),
                               std::numeric_limits<std::size_t>::max(),
                               candidate_score, candidate_total};
            last_move = pool_move;
            if (verbose) {
                std::cout << "Move " << move_number
                          << " pool_multi_swap cardinality=" << selected.size()
                          << " newcomers=" << newcomers
                          << " max_new=" << options.multi_swap_max_new
                          << " round=" << multi_swap_round
                          << " score=" << candidate_score << "/" << polygon_count
                          << " covered=" << candidate_total
                          << " m; returning to 1-swap descent.\n";
            }
            emit_snapshot(pool_move, false);
            if (timeline) {
                timeline->record("accepted_move", "greedy_multi_swap",
                                 pool_move.move_number, pool_move.cardinality, covered,
                                 std::numeric_limits<std::size_t>::max(),
                                 std::numeric_limits<std::size_t>::max(),
                                 multi_swap_round);
            }
        }
    }

    // Alternate two exact neighborhoods at the fixed final threshold:
    // repeated restricted-pool CP-SAT solves, then complete 1-swap descent whenever
    // the pool phase stalls.  Stop only when both neighborhoods fail to improve.
    if (options.pool_polish && selected.size() == options.k) {
        std::size_t polish_round = 0;
        bool cp_sat_available = true;
        while (true) {
            bool cp_sat_stalled = false;

            while (cp_sat_available) {
                if (options.polish_max_rounds != 0 && polish_round >= options.polish_max_rounds) {
                    cp_sat_available = false;
                    if (verbose) {
                        std::cout << "Pool polishing reached --polish-max-rounds="
                                  << options.polish_max_rounds << ". Running a final 1-swap descent.\n";
                    }
                    break;
                }

                ++polish_round;
                const int current_score = solution_score(scene, covered, options.threshold);
                const double current_total = sum_covered(covered);
                if (verbose) {
                    std::cout << "Pool-polish round " << polish_round << ": incumbent score="
                              << current_score << "/" << polygon_count
                              << ", covered=" << current_total << " m.\n";
                }

                std::size_t requested_extra = options.polish_extra_count;
                if (requested_extra == 0) {
                    requested_extra = static_cast<std::size_t>(
                        std::ceil(options.polish_extra_fraction *
                                  static_cast<double>(options.k)));
                }
                if (timeline) {
                    timeline->record("phase_start", "post_k_pool_build", move_number,
                                     selected.size(), covered,
                                     std::numeric_limits<std::size_t>::max(),
                                     std::numeric_limits<std::size_t>::max(), polish_round);
                }
                const std::vector<std::size_t> pool = build_polish_pool(
                    scene, cache, selected, cover_count, covered, options.k,
                    options.threshold, requested_extra, verbose, "post-k");
                if (pool.size() <= options.k) {
                    if (verbose) {
                        std::cout << "Pool polishing stalled: no additional candidate could be added.\n";
                    }
                    cp_sat_stalled = true;
                    break;
                }

                if (timeline) {
                    timeline->record("phase_start", "post_k_cp_sat", move_number,
                                     selected.size(), covered,
                                     std::numeric_limits<std::size_t>::max(),
                                     std::numeric_limits<std::size_t>::max(), polish_round);
                }
                PoolSolveResult pool_result = solve_pool_cp_sat(
                    scene, cache, options, pool, selected, options.k,
                    options.threshold, pool.size() - selected.size(),
                    options.polish_time_limit, "post-k", verbose);
                SelectionState candidate_state = evaluate_selection(
                    scene, cache, pool_result.selected_candidates);
                const int candidate_score = solution_score(
                    scene, candidate_state.covered, options.threshold);
                const double candidate_total = sum_covered(candidate_state.covered);
                const bool improves = candidate_score > current_score ||
                    (candidate_score == current_score &&
                     candidate_total > current_total + options.swap_improvement_epsilon);

                if (!improves) {
                    if (timeline) {
                        timeline->record("round_end_no_improvement", "post_k_cp_sat",
                                         move_number, selected.size(), covered,
                                         std::numeric_limits<std::size_t>::max(),
                                         std::numeric_limits<std::size_t>::max(), polish_round);
                    }
                    if (verbose) {
                        std::cout << "Pool polishing found no strict improvement (status="
                                  << pool_result.status << ").";
                        if (!pool_result.optimal) {
                            std::cout << " The solver did not prove optimality for this pool.";
                        }
                        std::cout << " Returning to complete 1-swap descent.\n";
                    }
                    cp_sat_stalled = true;
                    break;
                }

                selected = std::move(pool_result.selected_candidates);
                cover_count = std::move(candidate_state.cover_count);
                covered = std::move(candidate_state.covered);
                std::fill(selected_flag.begin(), selected_flag.end(), 0);
                for (const std::size_t candidate : selected) selected_flag[candidate] = 1;
                ++move_number;
                MoveInfo pool_move{move_number, "pool_cp_sat", selected.size(),
                                   std::numeric_limits<std::size_t>::max(),
                                   std::numeric_limits<std::size_t>::max(),
                                   std::numeric_limits<std::size_t>::max(),
                                   candidate_score, candidate_total};
                last_move = pool_move;
                if (verbose) {
                    std::cout << "Move " << move_number
                              << " pool_cp_sat cardinality=" << selected.size()
                              << " score=" << candidate_score << "/" << polygon_count
                              << " covered=" << candidate_total << " m.\n";
                }
                emit_snapshot(pool_move, false);
                if (timeline) {
                    timeline->record("accepted_move", "post_k_cp_sat", pool_move.move_number,
                                     pool_move.cardinality, covered,
                                     std::numeric_limits<std::size_t>::max(),
                                     std::numeric_limits<std::size_t>::max(), polish_round);
                }
            }

            const std::size_t accepted_swaps = run_swap_descent(
                options.threshold, "post_pool_swap");
            if (accepted_swaps == 0) {
                if (verbose) {
                    if (cp_sat_stalled) {
                        std::cout << "Alternating post-k search converged: neither CP-SAT pool "
                                     "polishing nor complete 1-swaps found a strict improvement.\n";
                    } else {
                        std::cout << "Post-k search stopped after the configured CP-SAT round limit; "
                                     "the final 1-swap descent found no strict improvement.\n";
                    }
                }
                break;
            }

            if (!cp_sat_available) {
                if (verbose) {
                    std::cout << "Final 1-swap descent accepted " << accepted_swaps
                              << " swaps, but no further CP-SAT rounds are allowed by "
                                 "--polish-max-rounds.\n";
                }
                break;
            }

            if (verbose) {
                std::cout << "1-swap descent accepted " << accepted_swaps
                          << " improvements; rebuilding a replacement-aware pool and returning to CP-SAT.\n";
            }
        }
    }

    if (last_move.has_value()) emit_snapshot(*last_move, true);
    if (timeline) {
        timeline->record("finished", "complete", move_number, selected.size(), covered);
    }
    return {std::move(selected), std::move(covered)};
}

struct ExponentEvaluation {
    double exponent = 1.0;
    int final_score = 0;
    double covered_total = 0.0;
    double covered_ratio = 0.0;
    double quality = 0.0;
    double seconds = 0.0;
    std::size_t selected_count = 0;
};

[[nodiscard]] bool better_evaluation(const ExponentEvaluation& a, const ExponentEvaluation& b) {
    if (a.final_score != b.final_score) return a.final_score > b.final_score;
    if (std::abs(a.covered_total - b.covered_total) > 1e-9) return a.covered_total > b.covered_total;
    return a.exponent < b.exponent;
}

[[nodiscard]] ExponentEvaluation evaluate_exponent(
    const Scene& scene,
    const VisibilityCache& cache,
    const CandidatePolygonMap& candidate_polygon_map,
    const CandidatePolygonReverseIndex& candidate_polygon_reverse_index,
    const VisibilityEngine& engine,
    const Options& options,
    double exponent,
    std::size_t threads_per_run)
{
#ifdef GIS_CUP_HAS_OPENMP
    omp_set_dynamic(0);
    omp_set_num_threads(static_cast<int>(std::max<std::size_t>(1, threads_per_run)));
#else
    (void)threads_per_run;
#endif
    const auto start = std::chrono::steady_clock::now();
    GreedyResult result = local_search_select(
        scene, cache, candidate_polygon_map, candidate_polygon_reverse_index,
        engine, options, exponent, false, false);
    const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    const double total_perimeter = std::accumulate(
        scene.polygons.begin(), scene.polygons.end(), 0.0,
        [](double sum, const PolygonData& polygon) { return sum + polygon.perimeter; });
    const double covered_total = sum_covered(result.covered_weight);
    const double covered_ratio = total_perimeter > 0.0
        ? std::clamp(covered_total / total_perimeter, 0.0, 1.0)
        : 0.0;
    const int final_score = solution_score(scene, result.covered_weight, options.threshold);
    // The fractional term is strictly below one, so one additional qualified polygon
    // always dominates any amount of secondary boundary coverage.
    const double quality = static_cast<double>(final_score) + 0.999 * covered_ratio;
    return {exponent, final_score, covered_total, covered_ratio, quality, seconds,
            result.selected_candidates.size()};
}

struct ExponentSearchResult {
    ExponentEvaluation best;
    std::vector<ExponentEvaluation> evaluations;
};

[[nodiscard]] ExponentSearchResult ternary_search_exponent(
    const Scene& scene,
    const VisibilityCache& cache,
    const CandidatePolygonMap& candidate_polygon_map,
    const CandidatePolygonReverseIndex& candidate_polygon_reverse_index,
    const VisibilityEngine& engine,
    const Options& options,
    std::size_t total_threads)
{
    const std::size_t worker_count = std::min<std::size_t>(2, std::max<std::size_t>(1, options.exponent_workers));
    const std::size_t active_workers = std::min(worker_count, std::max<std::size_t>(1, total_threads));
    const std::size_t threads_per_run = std::max<std::size_t>(1, total_threads / active_workers);
    std::map<double, ExponentEvaluation> memo;
    auto canonical_exponent = [](double exponent) {
        constexpr double scale = 1e12;
        return std::round(exponent * scale) / scale;
    };

    auto evaluate_one = [&](double exponent) {
        exponent = canonical_exponent(exponent);
        const auto found = memo.find(exponent);
        if (found != memo.end()) return found->second;
        ExponentEvaluation evaluation = evaluate_exponent(
            scene, cache, candidate_polygon_map, candidate_polygon_reverse_index,
            engine, options, exponent, threads_per_run);
        memo.emplace(exponent, evaluation);
        return evaluation;
    };

    auto evaluate_pair = [&](double left_exponent, double right_exponent) {
        left_exponent = canonical_exponent(left_exponent);
        right_exponent = canonical_exponent(right_exponent);
        std::optional<ExponentEvaluation> left;
        std::optional<ExponentEvaluation> right;
        const auto left_found = memo.find(left_exponent);
        const auto right_found = memo.find(right_exponent);
        if (left_found != memo.end()) left = left_found->second;
        if (right_found != memo.end()) right = right_found->second;

        if (!left && !right && active_workers >= 2) {
            auto left_future = std::async(std::launch::async, [&]() {
                return evaluate_exponent(
                    scene, cache, candidate_polygon_map,
                    candidate_polygon_reverse_index, engine, options,
                    left_exponent, threads_per_run);
            });
            auto right_future = std::async(std::launch::async, [&]() {
                return evaluate_exponent(
                    scene, cache, candidate_polygon_map,
                    candidate_polygon_reverse_index, engine, options,
                    right_exponent, threads_per_run);
            });
            left = left_future.get();
            right = right_future.get();
            memo.emplace(left_exponent, *left);
            memo.emplace(right_exponent, *right);
        } else {
            if (!left) left = evaluate_one(left_exponent);
            if (!right) right = evaluate_one(right_exponent);
        }
        return std::pair{*left, *right};
    };

    double low = options.exponent_min;
    double high = options.exponent_max;
    std::cout << "Searching curve exponent m in [" << low << ", " << high << "] using "
              << options.exponent_search_iterations << " ternary rounds, " << active_workers
              << " concurrent evaluations, and " << threads_per_run << " thread(s) per evaluation.\n";

    for (std::size_t iteration = 0; iteration < options.exponent_search_iterations; ++iteration) {
        const double left_exponent = canonical_exponent(low + (high - low) / 3.0);
        const double right_exponent = canonical_exponent(high - (high - low) / 3.0);
        const auto [left, right] = evaluate_pair(left_exponent, right_exponent);
        std::cout << "m-search round " << (iteration + 1) << "/" << options.exponent_search_iterations
                  << ": m=" << std::fixed << std::setprecision(6) << left.exponent
                  << " score=" << left.final_score << " coverage=" << (100.0 * left.covered_ratio)
                  << "% time=" << left.seconds << " s; m=" << right.exponent
                  << " score=" << right.final_score << " coverage=" << (100.0 * right.covered_ratio)
                  << "% time=" << right.seconds << " s.\n";
        if (left.quality + 1e-12 < right.quality) {
            low = left_exponent;
        } else {
            high = right_exponent;
        }
    }

    const double midpoint = canonical_exponent(0.5 * (low + high));
    const ExponentEvaluation middle = evaluate_one(midpoint);
    std::cout << "m-search midpoint: m=" << std::fixed << std::setprecision(6) << middle.exponent
              << " score=" << middle.final_score << " coverage=" << (100.0 * middle.covered_ratio)
              << "% time=" << middle.seconds << " s.\n";

    ExponentSearchResult result;
    result.evaluations.reserve(memo.size());
    for (const auto& [exponent, evaluation] : memo) {
        (void)exponent;
        result.evaluations.push_back(evaluation);
    }
    if (result.evaluations.empty()) throw std::runtime_error("Exponent search produced no evaluations");
    result.best = result.evaluations.front();
    for (const ExponentEvaluation& evaluation : result.evaluations) {
        if (better_evaluation(evaluation, result.best)) result.best = evaluation;
    }
    return result;
}

void write_exponent_search_outputs(
    const fs::path& output_dir,
    std::vector<ExponentEvaluation> evaluations,
    const ExponentEvaluation& best,
    double search_min,
    double search_max)
{
    std::sort(evaluations.begin(), evaluations.end(), [](const auto& a, const auto& b) {
        return a.exponent < b.exponent;
    });

    {
        std::ofstream csv(output_dir / "m_search.csv");
        if (!csv) throw std::runtime_error("Could not write m_search.csv");
        csv << "m,final_score,covered_ratio,covered_percent,covered_m,quality,selected_points,runtime_seconds,is_best\n";
        csv << std::setprecision(12);
        for (const auto& evaluation : evaluations) {
            csv << evaluation.exponent << ',' << evaluation.final_score << ','
                << evaluation.covered_ratio << ',' << (100.0 * evaluation.covered_ratio) << ','
                << evaluation.covered_total << ',' << evaluation.quality << ','
                << evaluation.selected_count << ',' << evaluation.seconds << ','
                << (std::abs(evaluation.exponent - best.exponent) <= 1e-12 ? 1 : 0) << '\n';
        }
    }

    constexpr double width = 1100.0;
    constexpr double height = 680.0;
    constexpr double left_margin = 95.0;
    constexpr double right_margin = 35.0;
    constexpr double top_margin = 70.0;
    constexpr double bottom_margin = 90.0;
    const double plot_width = width - left_margin - right_margin;
    const double plot_height = height - top_margin - bottom_margin;
    double y_min = evaluations.front().quality;
    double y_max = evaluations.front().quality;
    for (const auto& evaluation : evaluations) {
        y_min = std::min(y_min, evaluation.quality);
        y_max = std::max(y_max, evaluation.quality);
    }
    if (std::abs(y_max - y_min) < 1e-9) {
        y_min -= 0.5;
        y_max += 0.5;
    } else {
        const double padding = 0.08 * (y_max - y_min);
        y_min -= padding;
        y_max += padding;
    }
    const double x_min = search_min;
    const double x_max = search_max;
    auto sx = [&](double value) {
        return left_margin + (value - x_min) / (x_max - x_min) * plot_width;
    };
    auto sy = [&](double value) {
        return top_margin + (y_max - value) / (y_max - y_min) * plot_height;
    };

    std::ofstream svg(output_dir / "m_search.svg");
    if (!svg) throw std::runtime_error("Could not write m_search.svg");
    svg << std::fixed << std::setprecision(3);
    svg << "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"" << width
        << "\" height=\"" << height << "\" viewBox=\"0 0 " << width << ' ' << height << "\">\n";
    svg << "<rect width=\"100%\" height=\"100%\" fill=\"white\"/>\n";
    svg << "<text x=\"" << width / 2.0 << "\" y=\"35\" text-anchor=\"middle\" "
        << "font-family=\"sans-serif\" font-size=\"24\" font-weight=\"bold\">Curve exponent search</text>\n";
    svg << "<text x=\"" << width / 2.0 << "\" y=\"58\" text-anchor=\"middle\" "
        << "font-family=\"sans-serif\" font-size=\"13\">quality = qualified polygons + 0.999 × total covered-boundary ratio</text>\n";

    for (int i = 0; i <= 5; ++i) {
        const double fraction = static_cast<double>(i) / 5.0;
        const double value = y_min + fraction * (y_max - y_min);
        const double py = sy(value);
        svg << "<line x1=\"" << left_margin << "\" y1=\"" << py << "\" x2=\""
            << (left_margin + plot_width) << "\" y2=\"" << py
            << "\" stroke=\"#dddddd\" stroke-width=\"1\"/>\n";
        svg << "<text x=\"" << (left_margin - 12.0) << "\" y=\"" << (py + 5.0)
            << "\" text-anchor=\"end\" font-family=\"sans-serif\" font-size=\"12\">"
            << std::setprecision(2) << value << std::setprecision(3) << "</text>\n";
    }
    for (int i = 0; i <= 6; ++i) {
        const double fraction = static_cast<double>(i) / 6.0;
        const double value = x_min + fraction * (x_max - x_min);
        const double px = sx(value);
        svg << "<line x1=\"" << px << "\" y1=\"" << top_margin << "\" x2=\"" << px
            << "\" y2=\"" << (top_margin + plot_height)
            << "\" stroke=\"#eeeeee\" stroke-width=\"1\"/>\n";
        svg << "<text x=\"" << px << "\" y=\"" << (top_margin + plot_height + 25.0)
            << "\" text-anchor=\"middle\" font-family=\"sans-serif\" font-size=\"12\">"
            << std::setprecision(2) << value << std::setprecision(3) << "</text>\n";
    }
    svg << "<line x1=\"" << left_margin << "\" y1=\"" << top_margin << "\" x2=\"" << left_margin
        << "\" y2=\"" << (top_margin + plot_height) << "\" stroke=\"black\" stroke-width=\"1.5\"/>\n";
    svg << "<line x1=\"" << left_margin << "\" y1=\"" << (top_margin + plot_height) << "\" x2=\""
        << (left_margin + plot_width) << "\" y2=\"" << (top_margin + plot_height)
        << "\" stroke=\"black\" stroke-width=\"1.5\"/>\n";
    svg << "<text x=\"" << (left_margin + plot_width / 2.0) << "\" y=\"" << (height - 28.0)
        << "\" text-anchor=\"middle\" font-family=\"sans-serif\" font-size=\"16\">m</text>\n";
    svg << "<text x=\"24\" y=\"" << (top_margin + plot_height / 2.0)
        << "\" transform=\"rotate(-90 24 " << (top_margin + plot_height / 2.0)
        << ")\" text-anchor=\"middle\" font-family=\"sans-serif\" font-size=\"16\">output quality</text>\n";

    svg << "<polyline fill=\"none\" stroke=\"#2867b2\" stroke-width=\"3\" points=\"";
    for (const auto& evaluation : evaluations) svg << sx(evaluation.exponent) << ',' << sy(evaluation.quality) << ' ';
    svg << "\"/>\n";
    for (const auto& evaluation : evaluations) {
        const bool is_best = std::abs(evaluation.exponent - best.exponent) <= 1e-12;
        const double px = sx(evaluation.exponent);
        const double py = sy(evaluation.quality);
        svg << "<circle cx=\"" << px << "\" cy=\"" << py << "\" r=\"" << (is_best ? 7 : 5)
            << "\" fill=\"" << (is_best ? "#c62828" : "#2867b2") << "\"/>\n";
        svg << "<text x=\"" << px << "\" y=\"" << (py - 11.0)
            << "\" text-anchor=\"middle\" font-family=\"sans-serif\" font-size=\"11\">m="
            << std::setprecision(3) << evaluation.exponent << ", score=" << evaluation.final_score
            << "</text>\n";
    }
    svg << "</svg>\n";
}

void clear_step_snapshots(const fs::path& output_dir) {
    if (!fs::exists(output_dir)) return;
    constexpr std::array<std::string_view, 4> prefixes{
        "selected_step_", "visibility_step_", "coverage_step_", "qualified_step_"};
    for (const auto& entry : fs::directory_iterator(output_dir)) {
        if (!entry.is_regular_file()) continue;
        const std::string name = entry.path().filename().string();
        for (const std::string_view prefix : prefixes) {
            if (name.starts_with(prefix) && entry.path().extension() == ".geojson") {
                std::error_code ec;
                fs::remove(entry.path(), ec);
                break;
            }
        }
    }
}

int main(int argc, char** argv) {
    try {
        const Options options = parse_options(argc, argv);
        std::cout << "gis_cup_visibility revision " << PROGRAM_REVISION << ".\n";
#ifdef GIS_CUP_HAS_OPENMP
        if (options.threads > 0) omp_set_num_threads(static_cast<int>(options.threads));
#else
        if (options.threads > 0) std::cerr << "Warning: --threads ignored because OpenMP is unavailable.\n";
#endif
        fs::create_directories(options.output_dir);
        const auto load_start = std::chrono::steady_clock::now();
        Scene scene = load_scene(options);
        const double load_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - load_start).count();
        std::cout << "Loaded " << scene.polygons.size() << " polygons and "
                  << scene.segments.size() << " original boundary edges in "
                  << load_seconds << " s.\n";
        if (scene.polygons.empty() || scene.segments.empty()) {
            throw std::runtime_error("No usable polygon boundaries were read");
        }
        if (options.boundary_spacing != 1.0 || options.candidate_spacing != 1.0) {
            std::cerr << "Warning: --boundary-spacing and --candidate-spacing are ignored; "
                         "candidate and coverage discretization is visibility-induced.\n";
        }

        const VisibilityConfig visibility_config{options.bbox_padding};
        const auto geometry_start = std::chrono::steady_clock::now();

        ThreeStagePipelineAudit pipeline_audit;

        // Stage 1: compute one seed visibility polygon for every original
        // polygon vertex.  At this point the candidate vector is only a
        // temporary list of seed query locations.
        build_vertex_candidates(scene);
        pipeline_audit.seed_vertices = scene.candidates.size();
        std::cout << "Stage 1 seed visibility queries: " << scene.candidates.size()
                  << " original polygon vertices.\n";
        VisibilityEngine engine(scene, visibility_config);
        write_domain(scene, engine.domain_box(), options.output_dir);
        if (options.vertex_visibility_distance > 0.0) {
            std::cout << "Stage 1 visibility candidates are limited to "
                      << options.vertex_visibility_distance
                      << " m from each generating polygon vertex.\n";
        } else {
            std::cout << "Stage 1 visibility candidates are unlimited in distance.\n";
        }
        // Stage 2: convert the vertices of the seed visibility polygons into
        // the final boundary guard locations.  Only a visibility vertex caused
        // by collinearity with one of the seed vertex's two incident edges is
        // perturbed.  The perturbation is a boundary-preserving slide along the
        // edge on which that visibility vertex lands.  No other point is moved.
        build_visibility_polygon_vertex_candidates(
            scene,
            engine,
            options.boundary_epsilon,
            options.vertex_visibility_distance,
            options.progress_every);
        const std::size_t visibility_vertex_candidate_count = scene.candidates.size();
        const CandidateSubdivisionStats subdivision_stats =
            append_candidate_subdivisions(scene, options.candidate_subdivisions);
        pipeline_audit.visibility_vertex_candidates = visibility_vertex_candidate_count;
        pipeline_audit.subdivision_candidates = subdivision_stats.added_candidates;
        pipeline_audit.final_candidates = scene.candidates.size();
        pipeline_audit.incident_edge_slides = static_cast<std::size_t>(std::count_if(
            scene.candidates.begin(), scene.candidates.end(),
            [&](const std::uint32_t sample_id) {
                return scene.samples.at(sample_id).is_incident_edge_slide;
            }));
        validate_final_boundary_candidates(scene, pipeline_audit.final_candidates);
        engine.refresh_scene_points();
        std::cout << "Stage 2 final boundary candidates: " << scene.candidates.size()
                  << " total = " << visibility_vertex_candidate_count
                  << " visibility-polygon vertices plus "
                  << subdivision_stats.added_candidates
                  << " unchanged boundary subdivision points across "
                  << subdivision_stats.source_intervals << " intervals ("
                  << options.candidate_subdivisions
                  << " equal part(s) per interval); only incident-edge degeneracies "
                  << "use the " << options.boundary_epsilon
                  << " m landing-edge slide.\n";

        // Stage 3: compute a visibility polygon for every final boundary guard
        // candidate.  The vertices of these second-round visibility polygons are
        // used only as boundary breakpoints.  They partition the original edges
        // into weighted coverage intervals and are never appended as candidates.
        const std::vector<std::uint32_t> final_candidate_ids = scene.candidates;
        const std::size_t candidate_sample_count = scene.samples.size();
        SegmentBreakpoints candidate_visibility_breakpoints = collect_visibility_breakpoints(
            scene, engine, options.progress_every, "Final-candidate visibility", 0.0);
        pipeline_audit.second_round_boundary_endpoints = std::accumulate(
            candidate_visibility_breakpoints.begin(),
            candidate_visibility_breakpoints.end(),
            std::size_t{0},
            [](std::size_t total, const std::vector<double>& values) {
                return total + values.size();
            });
        if (scene.candidates != final_candidate_ids ||
            scene.samples.size() != candidate_sample_count) {
            throw std::runtime_error(
                "Three-stage pipeline invariant failed: visibility evaluation mutated the final candidate set");
        }
        append_coverage_samples_from_breakpoints(
            scene, std::move(candidate_visibility_breakpoints));
        const std::size_t coverage_interval_count = scene.samples.size() - candidate_sample_count;
        pipeline_audit.coverage_intervals = coverage_interval_count;
        validate_coverage_only_append(
            scene, final_candidate_ids, candidate_sample_count);
        engine.refresh_scene_points();
        rebuild_point_tree(scene);

        const double geometry_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - geometry_start).count();
        std::cout << "Three-stage pipeline audit: "
                  << pipeline_audit.seed_vertices << " original-vertex seed visibilities -> "
                  << pipeline_audit.visibility_vertex_candidates
                  << " visibility-derived boundary points + "
                  << pipeline_audit.subdivision_candidates << " subdivisions -> "
                  << pipeline_audit.final_candidates << " final guard candidates ("
                  << pipeline_audit.incident_edge_slides << " selective slides); "
                  << pipeline_audit.second_round_boundary_endpoints
                  << " second-round visibility endpoints produced "
                  << pipeline_audit.coverage_intervals
                  << " coverage intervals and zero recursive candidates.\n";
        std::cout << "Visibility-induced discretization ready: "
                  << scene.candidates.size() << " candidates and "
                  << coverage_interval_count << " weighted boundary intervals; CDT has "
                  << engine.triangulation_vertices() << " vertices and "
                  << engine.triangulation_faces() << " finite faces; total preprocessing "
                  << geometry_seconds << " s.\n";
        if (scene.candidates.empty() || coverage_interval_count == 0) {
            throw std::runtime_error("Visibility-induced discretization produced no usable candidates or intervals");
        }
        if (options.k > scene.candidates.size()) {
            throw std::runtime_error("k exceeds the number of visibility-induced candidate points");
        }
        if (options.write_samples) write_samples(scene, options.output_dir);

        VisibilityCache cache;
        bool loaded_cache = false;
        std::future<void> background_cache_save;
        std::optional<std::chrono::steady_clock::time_point> cache_save_start;
        if (!options.force_rebuild_cache) loaded_cache = cache.load(options.cache_path, scene, visibility_config);
        if (loaded_cache) {
            std::cout << "Loaded visibility cache " << options.cache_path << " ("
                      << static_cast<double>(cache.byte_size()) / (1024.0 * 1024.0) << " MiB, "
                      << cache.total_memberships() << " candidate-sample memberships).\n";
        } else {
            cache.build(scene, engine, options.progress_every);
            std::cout << "Built visibility cache (" << static_cast<double>(cache.byte_size()) / (1024.0 * 1024.0)
                      << " MiB, " << cache.total_memberships() << " candidate-sample memberships).\n";
            if (!options.cache_path.empty()) {
                cache_save_start = std::chrono::steady_clock::now();
                const fs::path cache_path = options.cache_path;
                std::cout << "Writing visibility cache in the background while evaluation views are prepared.\n";
                background_cache_save = std::async(
                    std::launch::async,
                    [&cache, &scene, visibility_config, cache_path]() {
                        cache.save(cache_path, scene, visibility_config);
                    });
            }
        }

        cache.prepare_for_evaluation(options.k, options.expand_cache_in_memory, options.deduplicate_candidates);
        if (!cache.expanded()) {
            std::cout << "Using compressed in-memory visibility rows; this saves RAM but decodes varints during move evaluation.\n";
        }
        std::cout << "Move evaluation will consider " << cache.search_candidate_count() << "/"
                  << scene.candidates.size() << " candidate representatives.\n";
        const CandidatePolygonMap candidate_polygon_map =
            build_candidate_polygon_map(scene, cache, true);
        const CandidatePolygonReverseIndex candidate_polygon_reverse_index =
            build_candidate_polygon_reverse_index(
                scene, cache, candidate_polygon_map, true);

        if (background_cache_save.valid()) {
            background_cache_save.get();
            const double save_seconds = cache_save_start.has_value()
                ? std::chrono::duration<double>(
                      std::chrono::steady_clock::now() - *cache_save_start).count()
                : 0.0;
            std::cout << "Background visibility-cache write completed in "
                      << save_seconds << " s: " << options.cache_path << "\n";
        }

        double chosen_exponent = options.curve_exponent;
        if (options.search_curve_exponent) {
            std::size_t total_threads = 1;
#ifdef GIS_CUP_HAS_OPENMP
            total_threads = options.threads > 0 ? options.threads : static_cast<std::size_t>(omp_get_max_threads());
#endif
            const ExponentSearchResult search = ternary_search_exponent(
                scene, cache, candidate_polygon_map,
                candidate_polygon_reverse_index, engine, options,
                std::max<std::size_t>(1, total_threads));
            chosen_exponent = search.best.exponent;
            write_exponent_search_outputs(options.output_dir, search.evaluations, search.best,
                                          options.exponent_min, options.exponent_max);
            std::cout << "Best measured m=" << std::fixed << std::setprecision(6) << chosen_exponent
                      << " with final score=" << search.best.final_score << "/" << scene.polygons.size()
                      << " and covered-boundary ratio=" << (100.0 * search.best.covered_ratio) << "%.\n";
            std::cout << "Wrote " << (options.output_dir / "m_search.csv") << " and "
                      << (options.output_dir / "m_search.svg") << ".\n";
#ifdef GIS_CUP_HAS_OPENMP
            omp_set_num_threads(static_cast<int>(std::max<std::size_t>(1, total_threads)));
#endif
        }

        clear_step_snapshots(options.output_dir);
        const GreedyResult result = local_search_select(
            scene, cache, candidate_polygon_map,
            candidate_polygon_reverse_index, engine, options,
            chosen_exponent, true, true);
        std::cout << "Selected " << result.selected_candidates.size() << " points after add/swap search"
                  << (options.pool_polish ? " and post-k pool polishing" : "")
                  << " with m=" << std::fixed << std::setprecision(6) << chosen_exponent << ". "
                  << "QGIS snapshots are in " << options.output_dir << "\n"
                  << "Optimization timeline is in " << options.timeline_path << "\n";
        if (options.cgal_verify) {
            const CgalVerificationResult verification = verify_solution_with_cgal(
                scene, engine.domain_box(), result.selected_candidates,
                result.covered_weight, options);
            if (!verification.feasible) return 2;
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << "\n\n" << usage();
        return 1;
    }
}
