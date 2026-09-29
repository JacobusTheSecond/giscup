#!/bin/bash
ROOT=${ROOT:-$(pwd)}
ENV=${ENV:-/home/tdz894/envs/gisvis}
THREADS=${THREADS:-${SLURM_CPUS_PER_TASK:-4}}
EXPECTED_REVISION=${EXPECTED_REVISION:-RINGARC36.1-SAMPLED-REMOVAL-EXPLORERS-HYBRID-EDGE-BROAD-SA-EXACT-WIGGLE}
BUILD_DIR=${BUILD_DIR:-build}
STAGING_DIR="${BUILD_DIR}.staging.$$"
OLD_DIR="${BUILD_DIR}.previous.$$"

cd "$ROOT" || exit 1
module purge || exit 1
module load mamba/23.3.1 || exit 1

if [ -x "$ENV/bin/x86_64-conda-linux-gnu-c++" ]; then
    CXX=${CXX:-$ENV/bin/x86_64-conda-linux-gnu-c++}
elif [ -x "$ENV/bin/g++" ]; then
    CXX=${CXX:-$ENV/bin/g++}
else
    CXX=${CXX:-g++}
fi

cleanup_staging() {
    rm -rf "$STAGING_DIR"
}
trap cleanup_staging EXIT
rm -rf "$STAGING_DIR"

mamba run -p "$ENV" --no-capture-output cmake \
    -S . -B "$STAGING_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$ENV" \
    -DCMAKE_CXX_COMPILER="$CXX" || exit 1

mamba run -p "$ENV" --no-capture-output \
    cmake --build "$STAGING_DIR" --parallel "$THREADS" || exit 1

NAME=gis_cup_visibility
BINARY="$STAGING_DIR/$NAME"
test -x "$BINARY" || {
    echo "ERROR: build did not create $BINARY"
    exit 2
}
if ! LC_ALL=C grep -a -F -q "$EXPECTED_REVISION" "$BINARY"; then
    echo "ERROR: $NAME does not contain expected revision: $EXPECTED_REVISION"
    exit 3
fi
SHA256=$(sha256sum "$BINARY" | awk '{print $1}')
printf '%s\t%s\t%s\t%s\n' \
    "$EXPECTED_REVISION" "$SHA256" "$NAME" "$(date -Iseconds)" \
    > "$STAGING_DIR/$NAME.revision"

rm -rf "$OLD_DIR"
if [ -e "$BUILD_DIR" ]; then
    mv "$BUILD_DIR" "$OLD_DIR"
fi
if ! mv "$STAGING_DIR" "$BUILD_DIR"; then
    [ ! -e "$BUILD_DIR" ] && [ -e "$OLD_DIR" ] && mv "$OLD_DIR" "$BUILD_DIR"
    echo "ERROR: failed to install the staged build"
    exit 4
fi
rm -rf "$OLD_DIR"
trap - EXIT

EXACT_SHA=$(awk 'NR==1 {print $2}' "$BUILD_DIR/gis_cup_visibility.revision")
cat <<EOF2
Build complete:
  single exact/search/verify executable: $ROOT/$BUILD_DIR/gis_cup_visibility
Embedded revision: $EXPECTED_REVISION
SHA256:           $EXACT_SHA
EOF2
