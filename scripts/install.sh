#!/bin/bash
# Install Python deps + Rust native extension (tune_native).
# Called by systemd ExecStartPre on every restart.
# Skips maturin rebuild if Rust sources haven't changed.
set -e
cd /opt/tune-server

# Python editable install
.venv/bin/pip install -q -e .

# Rust native acceleration (optional — server works without it)
CARGO_HOME=/home/bertrand/.cargo
STAMP_FILE=rust/target/.tune_native_stamp
RUST_SOURCES="rust/tune-core/src rust/tune-pyo3/src Cargo.toml"

if [ ! -f "$CARGO_HOME/env" ]; then
    echo "install.sh: no Rust toolchain, skipping tune_native"
    exit 0
fi

source "$CARGO_HOME/env"

# Check if Rust sources changed since last successful build
CURRENT_HASH=$(find $RUST_SOURCES -name '*.rs' -o -name 'Cargo.toml' 2>/dev/null | sort | xargs cat 2>/dev/null | md5sum | cut -d' ' -f1)
PREV_HASH=""
[ -f "$STAMP_FILE" ] && PREV_HASH=$(cat "$STAMP_FILE")

if [ "$CURRENT_HASH" = "$PREV_HASH" ] && .venv/bin/python -c "import tune_native" 2>/dev/null; then
    echo "install.sh: tune_native up to date, skipping rebuild"
    exit 0
fi

echo "install.sh: building tune_native..."
if .venv/bin/maturin develop -m rust/tune-pyo3/Cargo.toml --release -q 2>&1; then
    mkdir -p "$(dirname "$STAMP_FILE")"
    echo "$CURRENT_HASH" > "$STAMP_FILE"
    echo "install.sh: tune_native built successfully"
else
    echo "install.sh: tune_native build failed (non-fatal)"
fi
