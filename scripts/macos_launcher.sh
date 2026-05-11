#!/bin/bash
# macOS launcher for Tune Server
# Replaces the PyInstaller/rumps menubar wrapper that caused 2GB+ disk writes
# and 5-minute startup freezes due to PyObjC struct registration overhead.
#
# This script:
#   1. Launches the tune-server runtime in the background
#   2. Opens the web UI once the server is ready
#   3. Keeps the process alive so macOS sees the .app as running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME="${BUNDLE_DIR}/Resources/runtime/tune-server"
DATA_DIR="${HOME}/Library/Application Support/Tune Server"
LOG_DIR="${HOME}/Library/Logs"
LOG_FILE="${LOG_DIR}/Tune Server.log"

mkdir -p "$DATA_DIR" "$LOG_DIR"

# Read version from dist-info in the runtime's _internal folder
VERSION="unknown"
if [ -d "${BUNDLE_DIR}/Resources/runtime/_internal" ]; then
    for d in "${BUNDLE_DIR}/Resources/runtime/_internal"/tune_server-*.dist-info; do
        if [ -d "$d" ]; then
            base="$(basename "$d")"
            VERSION="${base#tune_server-}"
            VERSION="${VERSION%.dist-info}"
            break
        fi
    done
fi
export TUNE_VERSION="$VERSION"

# Data paths — keep DB and artwork in a writable location
export TUNE_DB_PATH="${TUNE_DB_PATH:-${DATA_DIR}/tune_server.db}"
export TUNE_ARTWORK_CACHE_DIR="${TUNE_ARTWORK_CACHE_DIR:-${DATA_DIR}/artwork_cache}"

echo "" >> "$LOG_FILE"
echo "=== Tune Server v${VERSION} starting at $(date -Iseconds) (data: ${DATA_DIR}) ===" >> "$LOG_FILE"

if [ ! -x "$RUNTIME" ]; then
    osascript -e "display dialog \"Tune Server: binaire introuvable.\" & return & \"${RUNTIME}\" buttons {\"OK\"} default button 1 with icon stop with title \"Tune Server\""
    exit 1
fi

cleanup() {
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Launch the server
cd "$DATA_DIR"
"$RUNTIME" >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Wait for the server to be ready, then open browser
(
    for i in $(seq 1 30); do
        if curl -s -o /dev/null -w '' http://localhost:8888/api/v1/system/status 2>/dev/null; then
            open "http://localhost:8888"
            exit 0
        fi
        sleep 1
    done
) &

# Keep alive — when the server exits, we exit too
wait "$SERVER_PID" 2>/dev/null
EXIT_CODE=$?
echo "=== Tune Server exited with code ${EXIT_CODE} at $(date -Iseconds) ===" >> "$LOG_FILE"
exit $EXIT_CODE
