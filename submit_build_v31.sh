#!/usr/bin/env bash
set -euo pipefail
ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
CPUS=${BUILD_CPUS:-48}
MEM_GB=${BUILD_MEM_GB:-64}
TIME=${BUILD_TIME:-00:30:00}
PARTITION=${PARTITION:-gpu}
mkdir -p "$ROOT/logs"
exec sbatch --parsable -p "$PARTITION" --cpus-per-task="$CPUS" --mem="${MEM_GB}G" --time="$TIME" \
  --job-name=r31-build --output="$ROOT/logs/r31-build-%j.log" --error="$ROOT/logs/r31-build-%j.log" \
  --export=ALL,ROOT="$ROOT",ENV="$ENV" \
  --wrap='cd "$ROOT"; THREADS="$SLURM_CPUS_PER_TASK" ./build_v31.sh'
