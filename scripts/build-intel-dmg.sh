#!/bin/bash
# ===========================================================================
# Tune Server — Intel DMG build script
# Run on .16 (MacBook Pro Intel) to produce a complete, self-contained DMG.
#
# Usage: bash scripts/build-intel-dmg.sh 0.7.84
#
# Produces: ~/Tune-Server-{VERSION}-intel.tar.gz
# Contains: Tune Server.app with:
#   - tune-server runtime (PyInstaller)
#   - ffmpeg bundled (static x86_64)
#   - launch.sh as main executable (bypasses rumps menubar)
#   - Démarrer Tune.command for drag-to-Desktop convenience
# ===========================================================================

set -e

VERSION="${1:?Usage: $0 <version>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONDA_PYTHON="$HOME/miniconda3/envs/py311/bin"

echo "=== Building Tune Server v${VERSION} Intel DMG ==="
cd "$REPO_DIR"

export PATH="$CONDA_PYTHON:$PATH"

# 1. Install deps
echo "[1/7] Installing dependencies..."
pip install -q -e . rumps pyinstaller

# 2. Build server binary
echo "[2/7] Building tune-server binary..."
rm -rf build dist "Tune Server.app"
pyinstaller --name tune-server --onedir --add-data "web:web" \
    --collect-all tune_server --noconfirm tune_server/__main__.py

# 3. Build menubar wrapper (kept as fallback)
echo "[3/7] Building menubar wrapper..."
pyinstaller --windowed --name "Tune Server" --icon scripts/AppIcon.icns \
    --osx-bundle-identifier fr.mozaiklabs.tune-server \
    --collect-all rumps --hidden-import rumps \
    --noconfirm scripts/macos_menubar.py

# 4. Assemble .app bundle
echo "[4/7] Assembling app bundle..."
mv "dist/Tune Server.app" "Tune Server.app"
mkdir -p "Tune Server.app/Contents/Resources/runtime"
cp -R dist/tune-server/* "Tune Server.app/Contents/Resources/runtime/"

# 5. Patch Info.plist
echo "[5/7] Patching Info.plist..."
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "Tune Server.app/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "Tune Server.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "Tune Server.app/Contents/Info.plist" 2>/dev/null || true

# 6. Replace main executable with direct launcher (bypass rumps)
echo "[6/7] Installing direct launcher (bypass rumps)..."
cat > "Tune Server.app/Contents/MacOS/launch.sh" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources/runtime" && pwd)"
DATA_DIR="$HOME/Library/Application Support/Tune Server"
mkdir -p "$DATA_DIR"
export TUNE_DB_PATH="$DATA_DIR/tune_server.db"
export TUNE_ARTWORK_CACHE_DIR="$DATA_DIR/artwork_cache"
# Add bundled ffmpeg to PATH
export PATH="$DIR:$PATH"
cd "$DATA_DIR"
(sleep 4 && open "http://localhost:8888") &
exec "$DIR/tune-server"
LAUNCHER
chmod +x "Tune Server.app/Contents/MacOS/launch.sh"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable launch.sh" "Tune Server.app/Contents/Info.plist"

# 7. Bundle ffmpeg (static x86_64)
echo "[7/7] Bundling ffmpeg..."
FFMPEG_PATH="Tune Server.app/Contents/Resources/runtime/ffmpeg"
if [ ! -f "$FFMPEG_PATH" ]; then
    FFMPEG_ZIP="/tmp/ffmpeg-static-intel.zip"
    if [ ! -f "$FFMPEG_ZIP" ]; then
        curl -L "https://evermeet.cx/ffmpeg/ffmpeg-7.1.1.zip" -o "$FFMPEG_ZIP"
    fi
    unzip -o "$FFMPEG_ZIP" -d "/tmp/ffmpeg-static"
    cp "/tmp/ffmpeg-static/ffmpeg" "$FFMPEG_PATH"
    chmod +x "$FFMPEG_PATH"
fi

# Package
echo "=== Packaging ==="
rm -f "$HOME/Tune-Server-${VERSION}-intel.tar.gz"
tar czf "$HOME/Tune-Server-${VERSION}-intel.tar.gz" "Tune Server.app"
ls -lh "$HOME/Tune-Server-${VERSION}-intel.tar.gz"

echo ""
echo "=== Done ==="
echo "Next steps (on M1):"
echo "  1. scp bertrand@192.168.1.16:~/Tune-Server-${VERSION}-intel.tar.gz /tmp/"
echo "  2. Sign + notarize + DMG (see reference_intel_dmg_build.md)"
