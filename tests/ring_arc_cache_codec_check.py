#!/usr/bin/env python3
"""Source/codec regression check for RINGARC28.

This test does not require CGAL/GDAL/OR-Tools. It models the exact cache codec,
checks ring-boundary splitting and randomized round trips, and verifies that the
production source contains the expected format and fast arc-consumer wiring.
"""
from __future__ import annotations

import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "src" / "main.cpp").read_text()

REQUIRED = [
    f'PROGRAM_REVISION = "{(ROOT / "VERSION").read_text().strip()}"',
    'Stage 2 memory-slim candidate assembly:',
    'Stage 2 streaming assembly:',
    'class CanonicalCandidateIdentitySet',
    'std::memcmp(magic, "GISVIS6", 7)',
    'expected_encoding_tag = 0x52494E4741524332ULL',
    'encode_visible_sample_arcs(scene, visible)',
    'validate_scene_coverage_layout(scene)',
    'validate_encoded_rows(scene, true)',
    'Visibility cache load: header accepted; allocating',
    'Visibility cache validation:',
    'cache payload checksum mismatch',
    'cache.for_each_arc(ci,',
]
missing = [token for token in REQUIRED if token not in SOURCE]
assert not missing, f"missing RINGARC28 source markers: {missing}"


def enc_varint(value: int) -> bytes:
    assert 0 <= value <= 0xFFFFFFFF
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def dec_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data) and shift <= 28:
        byte = data[pos]
        pos += 1
        payload = byte & 0x7F
        if shift == 28 and payload > 0x0F:
            raise ValueError("overflowing varint")
        result |= payload << shift
        if not byte & 0x80:
            if shift > 0 and payload == 0:
                raise ValueError("non-canonical varint")
            return result, pos
        shift += 7
    raise ValueError("corrupt varint")


def encode(ids: list[int], ring_of: list[tuple[int, int]]) -> tuple[bytes, list[tuple[int, int]]]:
    assert ids == sorted(set(ids))
    out = bytearray()
    arcs: list[tuple[int, int]] = []
    previous_start = 0
    first = True
    i = 0
    while i < len(ids):
        begin = end = ids[i]
        i += 1
        while i < len(ids) and ids[i] == end + 1 and ring_of[ids[i]] == ring_of[end]:
            end = ids[i]
            i += 1
        length = end - begin + 1
        out += enc_varint(begin if first else begin - previous_start)
        out += enc_varint(length - 1)
        arcs.append((begin, length))
        previous_start = begin
        first = False
    return bytes(out), arcs


def decode(data: bytes, sample_count: int) -> tuple[list[int], list[tuple[int, int]]]:
    pos = 0
    current_start = 0
    first = True
    ids: list[int] = []
    arcs: list[tuple[int, int]] = []
    while pos < len(data):
        delta, pos = dec_varint(data, pos)
        if pos >= len(data):
            raise ValueError("missing arc length")
        length_minus_one, pos = dec_varint(data, pos)
        if not first and delta == 0:
            raise ValueError("non-increasing start")
        start = delta if first else current_start + delta
        length = length_minus_one + 1
        if start > sample_count or length > sample_count - start:
            raise ValueError("arc out of bounds")
        arcs.append((start, length))
        ids.extend(range(start, start + length))
        current_start = start
        first = False
    return ids, arcs


# Explicit ring-boundary regression: globally consecutive IDs 2 and 3 must be
# split because they belong to different source rings.
ring_of = [(0, 0), (0, 0), (0, 0), (0, 1), (0, 1), (1, 0)]
payload, arcs = encode([1, 2, 3, 4, 5], ring_of)
assert arcs == [(1, 2), (3, 2), (5, 1)], arcs
assert decode(payload, len(ring_of))[0] == [1, 2, 3, 4, 5]

rng = random.Random(0x52494E4741524332)
for _ in range(20_000):
    ring_count = rng.randint(1, 20)
    ring_of: list[tuple[int, int]] = []
    polygon = 0
    for ring in range(ring_count):
        if ring and rng.random() < 0.35:
            polygon += 1
            local_ring = 0
        else:
            local_ring = ring if polygon == 0 else sum(1 for p, _ in ring_of if p == polygon)
        ring_of.extend([(polygon, local_ring)] * rng.randint(1, 80))
    ids = [i for i in range(len(ring_of)) if rng.random() < 0.35]
    payload, arcs = encode(ids, ring_of)
    decoded, decoded_arcs = decode(payload, len(ring_of))
    assert decoded == ids
    assert decoded_arcs == arcs
    for begin, length in arcs:
        assert ring_of[begin] == ring_of[begin + length - 1]

# Corruption checks matching production constraints.
try:
    decode(enc_varint(10) + enc_varint(100), 20)
except ValueError:
    pass
else:
    raise AssertionError("out-of-bounds arc was accepted")

try:
    decode(enc_varint(1) + enc_varint(0) + enc_varint(0) + enc_varint(0), 20)
except ValueError:
    pass
else:
    raise AssertionError("zero start delta after first arc was accepted")

for malformed in (bytes([0x80, 0x00]), bytes([0xFF, 0xFF, 0xFF, 0xFF, 0x10])):
    try:
        dec_varint(malformed, 0)
    except ValueError:
        pass
    else:
        raise AssertionError(f"malformed varint was accepted: {malformed!r}")

print("RINGARC28 codec/source checks passed: 20,000 randomized exact round trips")
