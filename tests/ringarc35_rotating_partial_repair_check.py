#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'src/main.cpp').read_text()
REV = (ROOT / 'VERSION').read_text().strip()

assert f'PROGRAM_REVISION = "{REV}"' in SRC

# The new policy is repair-only. The ordinary exact descent wrapper must still
# call the shared scanner with canonical ordering explicitly selected.
canonical_call = '''&stepped_add_cache, &pending_add_cache_changes,\n            options.threads, verbose, false, 0U);'''
assert canonical_call in SRC

# Known/predicted deadline-limited repair opts into ranked-block rotation; a
# first cold scan remains canonical until it actually demonstrates a timeout.
for needle in [
    'bool repair_previous_scan_was_partial = false;',
    'const bool likely_partial_repair =',
    'repair_previous_scan_was_partial ||',
    'predicted_scan_seconds > available_seconds',
    'likely_partial_repair,',
    'repair_rotating_rank_cursor);',
]:
    assert needle in SRC, needle

# Keep a small strongest-first prefix, then rotate only block ranks. No random
# candidate permutation is introduced.
for needle in [
    'constexpr std::size_t partial_elite_prefix_blocks = 4U;',
    'rotating_partial_block_rank_order(',
    'constexpr std::size_t max_batch_blocks = 8U;',
    'One block per exact evaluation call keeps the cursor',
    'search.rotating_next_rank = order_index + 1U;',
]:
    assert needle in SRC, needle
assert 'std::shuffle(scan_ranks' not in SRC
assert 'std::shuffle(batch_candidates' not in SRC

# Critical proof boundary: canonical globally sorted scans may prune the whole
# suffix; rotated scans may only prune the current block from its exact bound.
scanner = SRC[SRC.index('if (use_exact_cached_bounds) {'):]
scanner = scanner[:scanner.index('// Continuous-threshold/fallback mode')]
assert 'if (!use_rotating_order) {' in scanner
assert 'search.pruned_blocks += ordered_blocks.size() - order_index;' in scanner
assert "The current block's exact add upper bound is enough to" in scanner
assert '++search.pruned_blocks;' in scanner

# A completed scan remains a real full scan and resets partial-coverage state.
for needle in [
    'repair_previous_scan_was_partial = partial_scan;',
    '++result.repair_full_scans_completed;',
    'repair_rotating_rank_cursor = 0U;',
    'if (partial_scan) break;',
]:
    assert needle in SRC, needle

# Telemetry needed to audit whether the huge edge universe is actually being
# traversed rather than repeatedly revisiting one prefix.
for needle in [
    'repair_rotating_partial_scans',
    'repair_rotating_blocks_considered',
    'repair_rotating_last_start_rank',
    'repair_rotating_next_rank',
    'rotating_cursor=',
]:
    assert needle in SRC, needle

print(f'{REV} rotating partial-repair invariants: PASS')
