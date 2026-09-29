#!/usr/bin/env python3
import math

def cap(cardinality: int) -> int:
    effective = float(max(500, cardinality))
    bonus = round(5.0 * math.sqrt(500.0 / effective))
    return max(4, min(9, 4 + bonus))

assert cap(500) == 9, cap(500)
assert cap(5000) == 6, cap(5000)
assert cap(10000) == 5, cap(10000)
assert cap(100000) == 4, cap(100000)
assert cap(500) >= cap(5000) >= cap(10000) >= cap(100000)
print('RINGARC32 conservative cardinality repair depth scaling: PASS')
