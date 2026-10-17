#!/usr/bin/env bash
# Build and install the native libpostal C library + its model data (~2 GB),
# then the Python binding. Tested on Debian/Ubuntu (x86_64). See README for macOS.
#
# Usage: sudo ./scripts/install_libpostal.sh [DATA_DIR]   (default /opt/libpostal_data)
set -euo pipefail

DATA_DIR="${1:-/opt/libpostal_data}"
SRC_DIR="${LIBPOSTAL_SRC_DIR:-/opt/src/libpostal}"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends \
    build-essential curl git autoconf automake libtool pkg-config ca-certificates python3-dev
fi

if [ ! -d "$SRC_DIR" ]; then
  git clone --depth 1 https://github.com/openvenues/libpostal.git "$SRC_DIR"
fi
cd "$SRC_DIR"
./bootstrap.sh
# --disable-sse2 is required on ARM (Apple Silicon, Graviton); harmless elsewhere.
CONFIGURE_FLAGS="--datadir=$DATA_DIR"
case "$(uname -m)" in arm64|aarch64) CONFIGURE_FLAGS="$CONFIGURE_FLAGS --disable-sse2" ;; esac
./configure $CONFIGURE_FLAGS
make -j"$(nproc 2>/dev/null || echo 2)"     # also downloads the model data into DATA_DIR
make install
command -v ldconfig >/dev/null 2>&1 && ldconfig

# Python binding. --use-pep517 avoids a Debian setuptools 'install_layout' build error.
python3 -m pip install --use-pep517 postal

python3 - <<'EOF'
from postal.parser import parse_address
print("libpostal OK:", parse_address("12 Main St, Springfield IL 62701"))
EOF
