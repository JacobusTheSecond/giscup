#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <vector>

// Compact a pre-coverage candidate-only sample prefix without performing any
// arithmetic on the sample records. Surviving records are copied verbatim and
// only candidate IDs are remapped to the dense 0..N-1 prefix required by Stage 3.
template <class Sample>
void compact_candidate_prefix_verbatim(
    std::vector<Sample>& samples,
    std::vector<std::uint32_t>& candidates,
    const std::vector<std::uint32_t>& survivor_ids)
{
    if (samples.size() != candidates.size()) {
        throw std::runtime_error(
            "Candidate compaction requires a candidate-only pre-coverage scene");
    }
    if (survivor_ids.size() > samples.size()) {
        throw std::runtime_error(
            "Candidate compaction survivor count exceeds sample count");
    }
    if (survivor_ids.size() >
        static_cast<std::size_t>(std::numeric_limits<std::uint32_t>::max())) {
        throw std::runtime_error(
            "Candidate compaction exceeds 32-bit candidate IDs");
    }

    std::vector<Sample> compacted;
    compacted.reserve(survivor_ids.size());
    for (const std::uint32_t sample_id : survivor_ids) {
        if (sample_id >= samples.size()) {
            throw std::runtime_error(
                "Candidate compaction encountered an out-of-range survivor ID");
        }
        compacted.push_back(samples[sample_id]);
    }

    samples.swap(compacted);
    candidates.resize(samples.size());
    std::iota(candidates.begin(), candidates.end(), std::uint32_t{0});
}
