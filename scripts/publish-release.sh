#!/usr/bin/env bash
# publish-release.sh — Download CI assets from GitHub, upload to VPS, update DB
#
# Run AFTER the CI has finished building (check gh run list).
#
# Usage: bash scripts/publish-release.sh 0.7.92
#
# This replaces the unreliable webhook that downloads before GitHub finishes.

set -euo pipefail

VERSION="${1:?Usage: $0 <version>}"
VPS="root@156.67.31.98"
CONTAINER="mozaiklabs-app-1"
TMPDIR="/tmp/release-${VERSION}"
REPO="renesenses/tune-server-linux"
MIN_LINUX_SIZE=50000000  # 50MB minimum for Linux tar.gz

echo "=== Publishing Tune v${VERSION} to mozaiklabs.fr ==="

# 1. Wait for ALL CI runs to finish (especially the Release workflow)
echo "[1/5] Waiting for CI to finish..."
for i in $(seq 1 60); do
    RUNNING=$(gh run list --repo "$REPO" --limit 5 --json status -q '[.[] | select(.status == "in_progress" or .status == "queued")] | length' 2>/dev/null || echo "0")
    if [ "$RUNNING" = "0" ]; then
        echo "  All CI runs complete ✓"
        break
    fi
    echo "  $RUNNING run(s) still active — waiting 30s (attempt $i/60)..."
    sleep 30
done

# Extra: wait for all expected assets to appear (>=10 files)
echo "  Checking asset count..."
for i in $(seq 1 20); do
    ASSET_COUNT=$(gh release view "v${VERSION}" --repo "$REPO" --json assets -q '.assets | length' 2>/dev/null || echo "0")
    if [ "$ASSET_COUNT" -ge 10 ]; then
        echo "  $ASSET_COUNT assets found ✓"
        break
    fi
    echo "  Only $ASSET_COUNT assets — waiting 15s for uploads to complete..."
    sleep 15
done

# 2. Download all assets from GitHub
echo "[2/5] Downloading assets from GitHub..."
mkdir -p "$TMPDIR"
gh release download "v${VERSION}" --repo "$REPO" --dir "$TMPDIR" --clobber \
    --pattern "tune-server-${VERSION}-*"

# 3. Verify sizes
echo "[3/5] Verifying asset sizes..."
LINUX_FILE="$TMPDIR/tune-server-${VERSION}-linux.tar.gz"
if [ -f "$LINUX_FILE" ]; then
    LINUX_SIZE=$(stat -f%z "$LINUX_FILE" 2>/dev/null || stat -c%s "$LINUX_FILE" 2>/dev/null)
    if [ "$LINUX_SIZE" -lt "$MIN_LINUX_SIZE" ]; then
        echo "  ERROR: Linux tar.gz is only ${LINUX_SIZE} bytes (expected >50MB)"
        echo "  CI may not have finished uploading. Wait and retry."
        exit 1
    fi
    echo "  Linux: $(du -h "$LINUX_FILE" | cut -f1) ✓"
fi

for f in "$TMPDIR"/tune-server-${VERSION}-*; do
    [ -f "$f" ] && echo "  $(basename "$f"): $(du -h "$f" | cut -f1)"
done

# 4. Upload to VPS
echo "[4/5] Uploading to VPS..."
ssh "$VPS" "mkdir -p /storage/releases/${VERSION}"
scp "$TMPDIR"/tune-server-${VERSION}-* "$VPS:/storage/releases/${VERSION}/"
ssh "$VPS" "docker exec $CONTAINER mkdir -p /var/www/html/public/storage/releases/${VERSION}"
for f in "$TMPDIR"/tune-server-${VERSION}-*; do
    [ -f "$f" ] || continue
    fname=$(basename "$f")
    ssh "$VPS" "docker cp /storage/releases/${VERSION}/$fname $CONTAINER:/var/www/html/public/storage/releases/${VERSION}/"
done
echo "  Files uploaded ✓"

# 5. Update DB with correct sizes
echo "[5/5] Updating database..."
LINUX_SIZE=$(stat -f%z "$TMPDIR/tune-server-${VERSION}-linux.tar.gz" 2>/dev/null || echo 0)
MACOS_DMG_SIZE=$(stat -f%z "$TMPDIR/tune-server-${VERSION}-macos.dmg" 2>/dev/null || echo 0)
MACOS_TAR_SIZE=$(stat -f%z "$TMPDIR/tune-server-${VERSION}-macos.tar.gz" 2>/dev/null || echo 0)
WIN_SETUP_SIZE=$(stat -f%z "$TMPDIR/tune-server-${VERSION}-windows-setup.exe" 2>/dev/null || echo 0)
WIN_ZIP_SIZE=$(stat -f%z "$TMPDIR/tune-server-${VERSION}-windows.zip" 2>/dev/null || echo 0)

SLUG=$(echo "$VERSION" | tr -d '.')
ssh "$VPS" "docker exec $CONTAINER php artisan tinker --execute=\"
\\\$assets = json_encode([
    'linux' => [['name' => 'tune-server-${VERSION}-linux.tar.gz', 'url' => '/storage/releases/${VERSION}/tune-server-${VERSION}-linux.tar.gz', 'size' => ${LINUX_SIZE}]],
    'macos' => [
        ['name' => 'tune-server-${VERSION}-macos.dmg', 'url' => '/storage/releases/${VERSION}/tune-server-${VERSION}-macos.dmg', 'size' => ${MACOS_DMG_SIZE}],
        ['name' => 'tune-server-${VERSION}-macos.tar.gz', 'url' => '/storage/releases/${VERSION}/tune-server-${VERSION}-macos.tar.gz', 'size' => ${MACOS_TAR_SIZE}],
    ],
    'windows' => [
        ['name' => 'tune-server-${VERSION}-windows-setup.exe', 'url' => '/storage/releases/${VERSION}/tune-server-${VERSION}-windows-setup.exe', 'size' => ${WIN_SETUP_SIZE}],
        ['name' => 'tune-server-${VERSION}-windows.zip', 'url' => '/storage/releases/${VERSION}/tune-server-${VERSION}-windows.zip', 'size' => ${WIN_ZIP_SIZE}],
    ],
]);
\DB::table('forum_releases')->updateOrInsert(
    ['version' => '${VERSION}'],
    ['name' => 'v${VERSION}', 'slug' => 'tune-v-${SLUG}', 'description' => 'Release v${VERSION}', 'released_at' => now(), 'is_active' => true, 'order' => 999, 'download_assets' => \\\$assets, 'updated_at' => now()]
);
echo 'DB updated';
\""

echo ""
echo "=== Done — v${VERSION} published to mozaiklabs.fr ==="
echo "Verify: https://mozaiklabs.fr/download"
