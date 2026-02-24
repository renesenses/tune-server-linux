#!/bin/bash
# Build the tune-server .deb package
#
# Prerequisites (Debian/Ubuntu):
#   sudo apt-get install build-essential debhelper python3 python3-venv python3-pip
#
# Usage:
#   ./build-deb.sh
#
# The resulting .deb will be created in the parent directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Building tune-server .deb package..."
dpkg-buildpackage -us -uc -b

echo ""
echo "==> Build complete. Package:"
ls -lh ../tune-server_*.deb 2>/dev/null || echo "  (check parent directory for .deb file)"
