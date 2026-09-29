#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <vector>

namespace ringarc {

struct AdaptiveAddCostModel {
    struct Observation {
        double cardinality = 0.0;
        double seconds = 0.0;
    };

    std::deque<Observation> ordinary_observations;
    double rebuild_extra_ewma_seconds = 0.0;
    std::size_t rebuild_samples = 0;
    double smoothed_forecast_seconds = 0.0;
    double last_raw_forecast_seconds = 0.0;
    double last_rebuild_reserve_seconds = 0.0;
    std::size_t smoothed_at_first_cardinality = 0;
    bool has_smoothed_forecast = false;

    [[nodiscard]] double robust_ordinary_seconds_at(
        double cardinality) const
    {
        if (ordinary_observations.empty()) return 0.0;
        std::vector<double> durations;
        durations.reserve(ordinary_observations.size());
        for (const Observation& observation : ordinary_observations) {
            durations.push_back(observation.seconds);
        }
        std::sort(durations.begin(), durations.end());
        auto quantile = [&](double fraction) -> double {
            const std::size_t index = std::min(
                durations.size() - 1,
                static_cast<std::size_t>(std::floor(
                    fraction * static_cast<double>(durations.size() - 1))));
            return durations[index];
        };
        const double low = quantile(0.10);
        const double high = quantile(0.90);

        double sum_x = 0.0;
        double sum_y = 0.0;
        for (const Observation& observation : ordinary_observations) {
            sum_x += observation.cardinality;
            sum_y += std::clamp(observation.seconds, low, high);
        }
        const double n = static_cast<double>(ordinary_observations.size());
        const double mean_x = sum_x / n;
        const double mean_y = sum_y / n;
        double sxx = 0.0;
        double sxy = 0.0;
        for (const Observation& observation : ordinary_observations) {
            const double y = std::clamp(observation.seconds, low, high);
            const double dx = observation.cardinality - mean_x;
            sxx += dx * dx;
            sxy += dx * (y - mean_y);
        }
        const double slope = sxx > 1e-12
            ? std::max(0.0, sxy / sxx)
            : 0.0;
        const double intercept = std::max(0.0, mean_y - slope * mean_x);
        return std::max(0.0, intercept + slope * cardinality);
    }

    void observe(std::size_t cardinality,
                 double seconds,
                 bool stepped_cache_full_rebuild) {
        if (cardinality == 0 || !(seconds > 0.0) || !std::isfinite(seconds)) return;
        if (stepped_cache_full_rebuild) {
            // A threshold-step rebuild is a periodic fixed event, not evidence
            // that every remaining add round suddenly became expensive.  Keep
            // it out of the ordinary per-add regression and amortize only its
            // excess cost over the number of future step transitions.
            const double ordinary = robust_ordinary_seconds_at(
                static_cast<double>(cardinality));
            const double extra = std::max(0.0, seconds - ordinary);
            const double alpha = rebuild_samples < 3 ? 0.35 : 0.15;
            rebuild_extra_ewma_seconds = rebuild_samples == 0
                ? extra
                : alpha * extra + (1.0 - alpha) * rebuild_extra_ewma_seconds;
            ++rebuild_samples;
            return;
        }
        ordinary_observations.push_back(
            Observation{static_cast<double>(cardinality), seconds});
        constexpr std::size_t max_observations = 128;
        while (ordinary_observations.size() > max_observations) {
            ordinary_observations.pop_front();
        }
    }

    [[nodiscard]] std::size_t samples() const {
        return ordinary_observations.size() + rebuild_samples;
    }

    [[nodiscard]] std::size_t ordinary_samples() const {
        return ordinary_observations.size();
    }

    [[nodiscard]] double raw_forecast(
        std::size_t first_cardinality,
        std::size_t last_cardinality,
        std::size_t remaining_step_rebuilds) const
    {
        if (ordinary_observations.empty() || first_cardinality > last_cardinality) {
            return 0.0;
        }
        std::vector<double> durations;
        durations.reserve(ordinary_observations.size());
        for (const Observation& observation : ordinary_observations) {
            durations.push_back(observation.seconds);
        }
        std::sort(durations.begin(), durations.end());
        auto quantile = [&](double fraction) -> double {
            const std::size_t index = std::min(
                durations.size() - 1,
                static_cast<std::size_t>(std::floor(
                    fraction * static_cast<double>(durations.size() - 1))));
            return durations[index];
        };
        const double low = quantile(0.10);
        const double high = quantile(0.90);

        double sum_x = 0.0;
        double sum_y = 0.0;
        for (const Observation& observation : ordinary_observations) {
            sum_x += observation.cardinality;
            sum_y += std::clamp(observation.seconds, low, high);
        }
        const double n = static_cast<double>(ordinary_observations.size());
        const double mean_x = sum_x / n;
        const double mean_y = sum_y / n;
        double sxx = 0.0;
        double sxy = 0.0;
        for (const Observation& observation : ordinary_observations) {
            const double y = std::clamp(observation.seconds, low, high);
            const double dx = observation.cardinality - mean_x;
            sxx += dx * dx;
            sxy += dx * (y - mean_y);
        }
        const double slope = sxx > 1e-12
            ? std::max(0.0, sxy / sxx)
            : 0.0;
        const double intercept = std::max(0.0, mean_y - slope * mean_x);
        double residual_sq = 0.0;
        for (const Observation& observation : ordinary_observations) {
            const double y = std::clamp(observation.seconds, low, high);
            const double residual = y - (intercept + slope * observation.cardinality);
            residual_sq += residual * residual;
        }
        const double residual_rms = std::sqrt(residual_sq / n);
        const std::size_t count = last_cardinality - first_cardinality + 1;
        const long double sum_cardinalities =
            (static_cast<long double>(first_cardinality) +
             static_cast<long double>(last_cardinality)) *
            static_cast<long double>(count) / 2.0L;
        const double fitted =
            static_cast<double>(count) * intercept +
            static_cast<double>(sum_cardinalities) * slope;
        const double model_uncertainty = ordinary_observations.size() < 20
            ? 1.25 : 1.10;
        const double residual_reserve =
            1.5 * residual_rms * std::sqrt(static_cast<double>(count));
        const double rebuild_reserve = static_cast<double>(remaining_step_rebuilds) *
            std::max(0.0, rebuild_extra_ewma_seconds);
        return model_uncertainty * fitted + residual_reserve + rebuild_reserve;
    }

    [[nodiscard]] double forecast(
        std::size_t first_cardinality,
        std::size_t last_cardinality,
        std::size_t remaining_step_rebuilds)
    {
        const double raw = raw_forecast(
            first_cardinality, last_cardinality, remaining_step_rebuilds);
        last_raw_forecast_seconds = raw;
        last_rebuild_reserve_seconds =
            static_cast<double>(remaining_step_rebuilds) *
            std::max(0.0, rebuild_extra_ewma_seconds);
        if (!(raw > 0.0)) return raw;
        // Update at most once per add round.  Strong asymmetric smoothing and
        // a per-round slew limit prevent a single cache-step transition or
        // noisy incremental refresh from thrashing all optional phase cadences.
        if (!has_smoothed_forecast) {
            smoothed_forecast_seconds = raw;
            has_smoothed_forecast = true;
        } else if (smoothed_at_first_cardinality != first_cardinality) {
            const double alpha = raw > smoothed_forecast_seconds ? 0.12 : 0.08;
            double updated = alpha * raw +
                (1.0 - alpha) * smoothed_forecast_seconds;
            updated = std::clamp(
                updated,
                0.92 * smoothed_forecast_seconds,
                1.12 * smoothed_forecast_seconds);
            smoothed_forecast_seconds = updated;
        }
        smoothed_at_first_cardinality = first_cardinality;
        return smoothed_forecast_seconds;
    }
};

struct AdaptivePhaseAvailability {
    bool available = false;
    std::size_t affordable_streak = 0;
    std::size_t unaffordable_streak = 0;
    std::size_t last_update_round = 0;

    [[nodiscard]] bool update(std::size_t add_round,
                              double phase_seconds,
                              double minimum_useful_seconds) {
        if (last_update_round == add_round) return available;
        last_update_round = add_round;
        if (phase_seconds >= minimum_useful_seconds) {
            ++affordable_streak;
            unaffordable_streak = 0;
            // Require two consistent affordable forecasts before enabling a
            // phase that was unavailable.  This suppresses one-round rebounds.
            if (!available && affordable_streak >= 2) available = true;
        } else {
            affordable_streak = 0;
            ++unaffordable_streak;
            // Keep an already active phase eligible through two pessimistic
            // samples.  Hard per-phase/global deadlines still prevent spending
            // time that does not actually exist.
            if (available && unaffordable_streak >= 3) available = false;
        }
        return available;
    }
};

struct AdaptivePhaseSchedule {
    std::size_t last_attempt_round = 0;
    std::size_t next_due_round = 0;
    bool temporarily_unavailable = false;

    [[nodiscard]] bool refresh(std::size_t add_round,
                               std::size_t cadence,
                               std::size_t unavailable_sentinel) {
        if (cadence == unavailable_sentinel || cadence == 0) {
            // Forecast unavailability is transient.  Never overwrite history
            // with SIZE_MAX: the next add round recomputes from the current
            // cadence and can immediately catch up when surplus recovers.
            temporarily_unavailable = true;
            next_due_round = 0;
            return false;
        }
        temporarily_unavailable = false;
        if (last_attempt_round == 0) {
            next_due_round = cadence;
        } else if (last_attempt_round >
                   std::numeric_limits<std::size_t>::max() - cadence) {
            next_due_round = unavailable_sentinel;
            return false;
        } else {
            next_due_round = last_attempt_round + cadence;
        }
        // If a new forecast shortens the cadence below the elapsed rounds,
        // the phase is due now.  There is no alignment to multiples from zero.
        return add_round >= next_due_round;
    }

    void record_attempt(std::size_t add_round) {
        last_attempt_round = add_round;
        next_due_round = 0;
        temporarily_unavailable = false;
    }
};

} // namespace ringarc
