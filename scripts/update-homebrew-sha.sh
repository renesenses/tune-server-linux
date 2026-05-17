#!/bin/bash
# Update Homebrew formula SHA256 checksums after a release tag
# Usage: bash scripts/update-homebrew-sha.sh [version]
# If version is omitted, reads from pyproject.toml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TAP_DIR="${HOME}/DEV/homebrew-tap"

if [ -n "${1:-}" ]; then
    VERSION="$1"
else
    VERSION=$(python3 -c "import tomllib; f=open('${REPO_ROOT}/pyproject.toml','rb'); print(tomllib.load(f)['project']['version'])")
fi

echo "Updating Homebrew formula for v${VERSION}..."

FORMULA="${TAP_DIR}/Formula/tune-server.rb"
if [ ! -f "$FORMULA" ]; then
    echo "Cloning homebrew-tap..."
    git clone https://github.com/renesenses/homebrew-tap.git "$TAP_DIR"
fi

cd "$TAP_DIR"
git pull --ff-only origin main

echo "Fetching SHA256 for tune-server-linux v${VERSION}..."
SERVER_SHA=$(curl -sL "https://github.com/renesenses/tune-server-linux/archive/refs/tags/v${VERSION}.tar.gz" | shasum -a 256 | awk '{print $1}')

echo "Fetching SHA256 for tune-web-client v${VERSION}..."
WEB_SHA=$(curl -sL "https://github.com/renesenses/tune-web-client/archive/refs/tags/v${VERSION}.tar.gz" | shasum -a 256 | awk '{print $1}')

echo "  Server: ${SERVER_SHA}"
echo "  Web:    ${WEB_SHA}"

# Update version
sed -i '' "s|url \"https://github.com/renesenses/tune-server-linux/archive/refs/tags/v.*\.tar\.gz\"|url \"https://github.com/renesenses/tune-server-linux/archive/refs/tags/v${VERSION}.tar.gz\"|" "$FORMULA"
sed -i '' "s|version \".*\"|version \"${VERSION}\"|" "$FORMULA"

# Update server SHA (first sha256 in file, before resource block)
awk -v sha="$SERVER_SHA" '
    /sha256/ && !done && !/resource/ { sub(/sha256 "[^"]*"/, "sha256 \"" sha "\""); done=1 }
    { print }
' "$FORMULA" > "$FORMULA.tmp" && mv "$FORMULA.tmp" "$FORMULA"

# Update web client SHA (sha256 inside resource block)
awk -v sha="$WEB_SHA" '
    /resource "web-client"/ { in_resource=1 }
    /sha256/ && in_resource { sub(/sha256 "[^"]*"/, "sha256 \"" sha "\""); in_resource=0 }
    { print }
' "$FORMULA" > "$FORMULA.tmp" && mv "$FORMULA.tmp" "$FORMULA"

# Update release notes link
sed -i '' "s|releases/tag/v[0-9.]*|releases/tag/v${VERSION}|" "$FORMULA"

# Update caveats version
sed -i '' "s|Tune Server v[0-9.]* installed|Tune Server v${VERSION} installed|" "$FORMULA"

echo ""
echo "Formula updated. Diff:"
git diff Formula/tune-server.rb

echo ""
read -p "Push to origin? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    git add Formula/tune-server.rb
    git commit -m "bump: tune-server v${VERSION}"
    git push origin main
    echo "Pushed to homebrew-tap."
else
    echo "Skipped push. Run manually: cd ${TAP_DIR} && git add -A && git commit && git push"
fi
