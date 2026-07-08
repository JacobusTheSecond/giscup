#!/usr/bin/env bash
set -euo pipefail
python3 plot_optimization_timeline.py \
  output/optimization_timeline.csv \
  --live \
  --interval 5
