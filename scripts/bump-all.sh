#!/usr/bin/env bash
# bump-all.sh — single command to bump the Tune version across every repo.
#
# Each ecosystem keeps its own version file (pyproject.toml, package.json,
# pubspec.yaml, project.yml) — there is no portable way to derive them
# from one place without invasive build-tool changes. So this script is
# the source-of-truth: it reads the new version, validates the format,
# and rewrites every file in lock-step.
#
# Usage:
#   scripts/bump-all.sh 0.7.57
#
# Files touched (5):
#   1. tune-server-linux/pyproject.toml          — Python wheel + bundle (Linux/macOS/Windows)
#   2. tune-server-linux/scripts/tune-update.bat — Windows auto-update fallback
#   3. tune-web-client/package.json              — Svelte SPA
#   4. tune-server-flutter/pubspec.yaml          — Flutter (iOS + Android)
#   5. tune-server-ipados/Tune/project.yml       — SwiftUI (iPadOS / iOS / macOS)
#                                                  (also bumps CURRENT_PROJECT_VERSION +1)
#
# Does NOT commit, tag, or push — release decisions stay manual.
# Run `git diff` after to review.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>   (e.g. $0 0.7.57)" >&2
    exit 2
fi

VERSION="$1"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: version must be X.Y.Z (got: $VERSION)" >&2
    exit 2
fi

DEV="${TUNE_DEV_DIR:-$HOME/DEV}"
PYPROJECT="$DEV/tune-server-linux/pyproject.toml"
BAT="$DEV/tune-server-linux/scripts/tune-update.bat"
WEB="$DEV/tune-web-client/package.json"
FLUTTER="$DEV/tune-server-flutter/pubspec.yaml"
IPAD="$DEV/tune-server-ipados/Tune/project.yml"
RUST="$DEV/tune-server-rust/Cargo.toml"

for f in "$PYPROJECT" "$BAT" "$WEB" "$FLUTTER" "$IPAD" "$RUST"; do
    [ -f "$f" ] || { echo "Error: missing $f" >&2; exit 1; }
done

# 0. Cargo.toml (workspace) — version = "X.Y.Z"
sed -i.bak -E "s/^version = \"[0-9]+\\.[0-9]+\\.[0-9]+\"/version = \"$VERSION\"/" "$RUST" && rm "$RUST.bak"

# 1. pyproject.toml — single line: version = "X.Y.Z"
sed -i.bak -E "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT" && rm "$PYPROJECT.bak"

# 2. tune-update.bat — two stale references to the previous version on
#    consecutive lines (PowerShell fallback + cmd fallback). Replace the
#    last X.Y.Z literal that precedes "set "VERSION=" so we don't touch
#    other version-shaped strings.
sed -i.bak -E "s/Write-Output '[0-9]+\\.[0-9]+\\.[0-9]+'/Write-Output '$VERSION'/" "$BAT"
sed -i.bak -E "s/(set \"VERSION=)[0-9]+\\.[0-9]+\\.[0-9]+/\\1$VERSION/" "$BAT"
rm -f "$BAT.bak"

# 3. package.json — "version": "X.Y.Z"
sed -i.bak -E "s/\"version\": \"[0-9]+\\.[0-9]+\\.[0-9]+\"/\"version\": \"$VERSION\"/" "$WEB" && rm "$WEB.bak"

# 4. pubspec.yaml — version: X.Y.Z+N (preserve +N build number)
if grep -qE "^version: [0-9]+\\.[0-9]+\\.[0-9]+\\+[0-9]+" "$FLUTTER"; then
    sed -i.bak -E "s/^version: [0-9]+\\.[0-9]+\\.[0-9]+(\\+[0-9]+)/version: $VERSION\\1/" "$FLUTTER"
else
    sed -i.bak -E "s/^version: [0-9]+\\.[0-9]+\\.[0-9]+.*/version: $VERSION+1/" "$FLUTTER"
fi
rm "$FLUTTER.bak"

# 5. project.yml — MARKETING_VERSION + CURRENT_PROJECT_VERSION (++).
#    The Swift target appears twice (Tune + TuneWatch) — both must move
#    together so TestFlight doesn't reject mismatched build numbers
#    against the same bundle id.
sed -i.bak -E "s/MARKETING_VERSION: \"[0-9]+\\.[0-9]+\\.[0-9]+\"/MARKETING_VERSION: \"$VERSION\"/g" "$IPAD"
# CURRENT_PROJECT_VERSION must monotonically increase. Read the current
# max, add 1, write back to every occurrence.
CURRENT_BUILD=$(grep -oE "CURRENT_PROJECT_VERSION: [0-9]+" "$IPAD" | head -1 | awk '{print $2}')
NEXT_BUILD=$((CURRENT_BUILD + 1))
sed -i.bak -E "s/CURRENT_PROJECT_VERSION: [0-9]+/CURRENT_PROJECT_VERSION: $NEXT_BUILD/g" "$IPAD"
rm "$IPAD.bak"

# 6. Homebrew formula — update version in local formula + push to tap repo
FORMULA="$DEV/tune-server-linux/homebrew/tune-server.rb"
if [ -f "$FORMULA" ]; then
    sed -i.bak -E "s/^  version \"[0-9]+\\.[0-9]+\\.[0-9]+\"/  version \"$VERSION\"/" "$FORMULA"
    sed -i.bak -E "s|/v[0-9]+\\.[0-9]+\\.[0-9]+/|/v$VERSION/|g" "$FORMULA"
    sed -i.bak -E "s/v[0-9]+\\.[0-9]+\\.[0-9]+\\.tar\\.gz/v$VERSION.tar.gz/" "$FORMULA"
    sed -i.bak -E "s/Tune Server v[0-9]+\\.[0-9]+\\.[0-9]+/Tune Server v$VERSION/" "$FORMULA"
    rm -f "$FORMULA.bak"
    echo "  - $FORMULA"

    # Push to homebrew-tap repo via GitHub API (if gh is available)
    if command -v gh &>/dev/null; then
        TAP_SHA=$(gh api repos/renesenses/homebrew-tap/contents/Formula/tune-server.rb -q '.sha' 2>/dev/null || true)
        if [ -n "$TAP_SHA" ]; then
            CONTENT=$(base64 -i "$FORMULA")
            gh api -X PUT repos/renesenses/homebrew-tap/contents/Formula/tune-server.rb \
                -f message="Update tune-server formula to v$VERSION" \
                -f content="$CONTENT" \
                -f sha="$TAP_SHA" >/dev/null 2>&1 \
                && echo "  - homebrew-tap updated on GitHub" \
                || echo "  ! homebrew-tap update failed (push manually)"
        fi
    fi
fi

echo
echo "Bumped Tune to v$VERSION (build $NEXT_BUILD for Apple targets)"
echo "  - $RUST"
echo "  - $PYPROJECT"
echo "  - $BAT"
echo "  - $WEB"
echo "  - $FLUTTER"
echo "  - $IPAD"
echo
echo "Review with: git diff"
echo "Then commit + tag per repo (release.sh handles that)."
echo "After tagging, run: bash scripts/release-forum-post.sh"
