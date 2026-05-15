#!/usr/bin/env bash
# ===========================================================================
# Tune Server — Apple release script (TestFlight + Intel DMG)
#
# Automates the entire macOS/iOS release pipeline from a single command:
#   Phase 1: TestFlight upload (iOS + macOS via xcodebuild)
#   Phase 2: Intel DMG (build on .16, sign/notarize on M1)
#   Phase 3: Upload to GitHub Release + VPS
#
# Usage:
#   bash scripts/release-apple.sh 0.7.85
#   bash scripts/release-apple.sh 0.7.85 --skip-testflight
#   bash scripts/release-apple.sh 0.7.85 --skip-intel
#   bash scripts/release-apple.sh 0.7.85 --intel-only
#   bash scripts/release-apple.sh 0.7.85 --testflight-only
#
# Prerequisites:
#   - Xcode + xcodegen installed
#   - Signing identities in keychain:
#       "Apple Distribution: Bertrand Clech (VV3696M7PL)"  (TestFlight)
#       "Developer ID Application: Bertrand Clech (VV3696M7PL)"  (DMG)
#   - Notarization profile: xcrun notarytool store-credentials "tune-notarytool"
#   - sshpass installed (brew install esolitos/ipa/sshpass)
#   - create-dmg installed (brew install create-dmg)
#   - gh CLI authenticated (for GitHub release upload)
#
# Environment variables (optional overrides):
#   INTEL_HOST        — SSH host for Intel build (default: bertrand@192.168.1.16)
#   INTEL_PASSWORD    — SSH password (default: read from keychain or prompt)
#   SIGNING_IDENTITY  — codesign identity (default: auto-detect "Developer ID Application")
#   NOTARY_PROFILE    — notarytool keychain profile (default: tune-notarytool)
#   IPADOS_DIR        — path to tune-server-ipados/Tune (default: ~/DEV/tune-server-ipados/Tune)
#   VPS_HOST          — VPS for mozaiklabs.fr (default: root@156.67.31.98)
#
# Log: /tmp/tune-release-apple.log
# ===========================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors + helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

LOG="/tmp/tune-release-apple.log"
: > "$LOG"  # truncate

log()    { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
ok()     { echo -e "  ${GREEN}✓${NC} $*" | tee -a "$LOG"; }
fail()   { echo -e "  ${RED}✗${NC} $*" | tee -a "$LOG"; }
warn()   { echo -e "  ${YELLOW}⚠${NC} $*" | tee -a "$LOG"; }
header() { echo -e "\n${BOLD}${BLUE}=== $* ===${NC}" | tee -a "$LOG"; }

die() { fail "$*"; echo "Full log: $LOG"; exit 1; }

# Track what succeeded/failed for summary
declare -a RESULTS=()
result() { RESULTS+=("$1"); }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
VERSION="${1:?Usage: $0 <version> [--skip-testflight] [--skip-intel] [--intel-only] [--testflight-only]}"
shift

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "Version must be X.Y.Z (got: $VERSION)"
fi

DO_TESTFLIGHT=true
DO_INTEL=true
DO_UPLOAD=true

for arg in "$@"; do
    case "$arg" in
        --skip-testflight)  DO_TESTFLIGHT=false ;;
        --skip-intel)       DO_INTEL=false ;;
        --intel-only)       DO_TESTFLIGHT=false; DO_UPLOAD=true ;;
        --testflight-only)  DO_INTEL=false; DO_UPLOAD=false ;;
        *) die "Unknown option: $arg" ;;
    esac
done

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IPADOS_DIR="${IPADOS_DIR:-$HOME/DEV/tune-server-ipados/Tune}"
EXPORT_PLIST="$IPADOS_DIR/ExportOptions.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INTEL_HOST="${INTEL_HOST:-bertrand@192.168.1.16}"
INTEL_IP="${INTEL_HOST##*@}"
NOTARY_PROFILE="${NOTARY_PROFILE:-tune-notarytool}"
VPS_HOST="${VPS_HOST:-root@156.67.31.98}"

# Auto-detect signing identity from keychain
if [ -z "${SIGNING_IDENTITY:-}" ]; then
    SIGNING_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
        | grep "Developer ID Application" \
        | head -1 \
        | sed 's/.*"\(.*\)"/\1/' || true)
    if [ -z "$SIGNING_IDENTITY" ]; then
        die "No 'Developer ID Application' identity found in keychain"
    fi
fi

# Resolve Intel password: env > keychain > hardcoded fallback
if [ -z "${INTEL_PASSWORD:-}" ]; then
    INTEL_PASSWORD=$(security find-generic-password -a "bertrand" -s "intel-mac" -w 2>/dev/null || true)
fi

# Temp directory for this release
WORK="/tmp/tune-release-${VERSION}"
rm -rf "$WORK"
mkdir -p "$WORK"

DMG_NAME="tune-server-${VERSION}-macos-intel.dmg"
APP_NAME="Tune Server"
APP_DIR="$WORK/${APP_NAME}.app"

header "Tune Server v${VERSION} — Apple Release"
log "TestFlight: $DO_TESTFLIGHT | Intel DMG: $DO_INTEL | Upload: $DO_UPLOAD"
log "Signing identity: $SIGNING_IDENTITY"
log "Work directory: $WORK"
log "Log file: $LOG"

# ---------------------------------------------------------------------------
# Phase 1: TestFlight (iOS + macOS)
# ---------------------------------------------------------------------------
if [ "$DO_TESTFLIGHT" = true ]; then
    header "Phase 1: TestFlight"

    # Verify prerequisites
    [ -d "$IPADOS_DIR" ] || die "iPadOS directory not found: $IPADOS_DIR"
    [ -f "$EXPORT_PLIST" ] || die "ExportOptions.plist not found: $EXPORT_PLIST"
    command -v xcodegen >/dev/null 2>&1 || die "xcodegen not installed"

    # Clean DerivedData to avoid iOS/macOS conflicts
    log "Cleaning DerivedData..."
    rm -rf "$HOME/Library/Developer/Xcode/DerivedData/Tune-*"
    ok "DerivedData cleaned"

    # Remove stale archives
    rm -rf /tmp/Tune_iOS.xcarchive /tmp/Tune_macOS.xcarchive
    rm -rf /tmp/Tune_iOS_export /tmp/Tune_macOS_export

    # 1. Regenerate Xcode project
    log "Regenerating Xcode project (xcodegen)..."
    (cd "$IPADOS_DIR" && xcodegen generate) >> "$LOG" 2>&1
    ok "xcodegen generate"

    # 2. Archive iOS
    log "Archiving iOS (this takes a few minutes)..."
    xcodebuild -project "$IPADOS_DIR/Tune.xcodeproj" \
        -scheme Tune_iOS \
        -configuration Release \
        -archivePath /tmp/Tune_iOS.xcarchive \
        archive -quiet >> "$LOG" 2>&1 \
        && ok "iOS archive" \
        || { fail "iOS archive failed"; result "FAIL: iOS archive"; }

    # 3. Export iOS to TestFlight
    if [ -d /tmp/Tune_iOS.xcarchive ]; then
        log "Exporting iOS to TestFlight..."
        xcodebuild -exportArchive \
            -archivePath /tmp/Tune_iOS.xcarchive \
            -exportPath /tmp/Tune_iOS_export \
            -exportOptionsPlist "$EXPORT_PLIST" >> "$LOG" 2>&1 \
            && ok "iOS TestFlight upload" \
            || {
                # Check for "Redundant Binary Upload" — not actually an error
                if grep -q "redundant binary upload" "$LOG" 2>/dev/null; then
                    warn "iOS: Redundant binary upload (same build already on TestFlight)"
                    result "SKIP: iOS TestFlight (redundant binary)"
                else
                    fail "iOS TestFlight export failed"
                    result "FAIL: iOS TestFlight"
                fi
            }
    fi

    # 4. Archive macOS — CRITICAL: must specify destination
    log "Archiving macOS (with explicit destination)..."
    xcodebuild -project "$IPADOS_DIR/Tune.xcodeproj" \
        -scheme Tune_macOS \
        -configuration Release \
        -destination "generic/platform=macOS" \
        -archivePath /tmp/Tune_macOS.xcarchive \
        archive -quiet >> "$LOG" 2>&1 \
        && ok "macOS archive" \
        || { fail "macOS archive failed"; result "FAIL: macOS archive"; }

    # 5. Verify it's actually macOS (not iOS structure)
    if [ -d /tmp/Tune_macOS.xcarchive ]; then
        if ls "/tmp/Tune_macOS.xcarchive/Products/Applications/Tune Server.app/Contents/MacOS/" >/dev/null 2>&1; then
            ok "macOS archive verified (has Contents/MacOS structure)"
        else
            die "macOS archive has iOS structure! The -destination flag was ignored. Check xcodebuild output in $LOG"
        fi

        # 6. Export macOS to TestFlight
        log "Exporting macOS to TestFlight..."
        xcodebuild -exportArchive \
            -archivePath /tmp/Tune_macOS.xcarchive \
            -exportPath /tmp/Tune_macOS_export \
            -exportOptionsPlist "$EXPORT_PLIST" >> "$LOG" 2>&1 \
            && ok "macOS TestFlight upload" \
            || {
                if grep -q "redundant binary upload" "$LOG" 2>/dev/null; then
                    warn "macOS: Redundant binary upload (same build already on TestFlight)"
                    result "SKIP: macOS TestFlight (redundant binary)"
                else
                    fail "macOS TestFlight export failed"
                    result "FAIL: macOS TestFlight"
                fi
            }
    fi

    result "OK: TestFlight phase completed"
else
    log "Skipping TestFlight phase"
fi

# ---------------------------------------------------------------------------
# Phase 2: Intel DMG
# ---------------------------------------------------------------------------
if [ "$DO_INTEL" = true ]; then
    header "Phase 2: Intel DMG"

    # 2a. Check if .16 is reachable
    if ! ping -c1 -t3 "$INTEL_IP" >/dev/null 2>&1; then
        warn ".16 ($INTEL_IP) not reachable — skipping Intel DMG"
        result "SKIP: Intel DMG (.16 unreachable)"
    else
        ok ".16 is reachable"

        # Resolve SSH command
        if [ -n "${INTEL_PASSWORD:-}" ]; then
            if ! command -v sshpass >/dev/null 2>&1; then
                die "sshpass not installed. Install: brew install esolitos/ipa/sshpass"
            fi
            SSH_CMD="sshpass -p $INTEL_PASSWORD ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no"
            SCP_CMD="sshpass -p $INTEL_PASSWORD scp -o PubkeyAuthentication=no -o StrictHostKeyChecking=no"
        else
            # Try key-based auth
            SSH_CMD="ssh -o StrictHostKeyChecking=no"
            SCP_CMD="scp -o StrictHostKeyChecking=no"
        fi

        # 2b. Build on .16
        log "Building Intel binary on .16 (this takes ~10 minutes)..."
        REMOTE_CMDS="cd ~/DEV/tune-server-linux && git fetch --all && git checkout v${VERSION} 2>/dev/null || git pull && bash scripts/build-intel-dmg.sh ${VERSION}"
        $SSH_CMD "$INTEL_HOST" "$REMOTE_CMDS" >> "$LOG" 2>&1 \
            && ok "Intel build on .16" \
            || die "Intel build failed on .16. Check $LOG"

        # 2c. SCP the tar.gz back
        log "Transferring Intel archive from .16..."
        INTEL_TAR="/tmp/Tune-Server-${VERSION}-intel.tar.gz"
        $SCP_CMD "${INTEL_HOST}:~/Tune-Server-${VERSION}-intel.tar.gz" "$INTEL_TAR" >> "$LOG" 2>&1 \
            && ok "SCP transfer ($(du -h "$INTEL_TAR" | cut -f1))" \
            || die "SCP transfer failed"

        # 2d. Extract
        log "Extracting Intel archive..."
        cd "$WORK"
        tar xzf "$INTEL_TAR"
        [ -d "$WORK/Tune Server.app" ] || die "Extracted archive missing 'Tune Server.app'"
        APP_DIR="$WORK/Tune Server.app"
        ok "Extracted"

        # 2e. Sign all Mach-O binaries
        header "Signing Intel .app"

        SIGN_FLAGS=(
            --force
            --timestamp
            --options runtime
            --sign "$SIGNING_IDENTITY"
        )

        # Count Mach-O files for progress
        MACHO_COUNT=$(find "$APP_DIR" -type f ! -path '*.framework/*' -exec sh -c 'file -b "$1" 2>/dev/null | grep -q "Mach-O"' _ {} \; -print | wc -l | tr -d ' ')
        log "Found $MACHO_COUNT Mach-O binaries to sign"

        # Sign embedded frameworks first (deepest-first)
        FRAMEWORK_COUNT=0
        find "$APP_DIR" -type d -name '*.framework' | sort -r | while read -r fw; do
            codesign --force --deep --timestamp --options runtime \
                --sign "$SIGNING_IDENTITY" "$fw" >> "$LOG" 2>&1
        done
        ok "Frameworks signed"

        # Sign every Mach-O (excluding frameworks, already done)
        SIGNED=0
        SIGN_ERRORS=0
        find "$APP_DIR" -type f ! -path '*.framework/*' -print0 | while IFS= read -r -d '' f; do
            [ -h "$f" ] && continue
            if file -b "$f" 2>/dev/null | grep -q 'Mach-O'; then
                if codesign "${SIGN_FLAGS[@]}" "$f" >> "$LOG" 2>&1; then
                    SIGNED=$((SIGNED + 1))
                else
                    echo "  Sign failed: $f" >> "$LOG"
                    SIGN_ERRORS=$((SIGN_ERRORS + 1))
                fi
            fi
        done
        ok "Mach-O binaries signed"

        # Sign the .app bundle itself (NOT --deep)
        codesign "${SIGN_FLAGS[@]}" "$APP_DIR" >> "$LOG" 2>&1 \
            && ok ".app bundle signed" \
            || die ".app bundle signing failed"

        # Verify signature
        log "Verifying signature..."
        codesign --verify --strict --verbose=2 "$APP_DIR" >> "$LOG" 2>&1 \
            && ok "Signature verification passed" \
            || die "Signature verification FAILED"

        # spctl check (may warn in some environments, not fatal)
        if spctl --assess --type execute "$APP_DIR" >> "$LOG" 2>&1; then
            ok "Gatekeeper assessment passed"
        else
            warn "spctl assess warning (may be OK before notarization)"
        fi

        # 2f. Notarize .app
        header "Notarizing Intel .app"
        log "Zipping .app for notarization..."
        APP_ZIP="$WORK/Tune-Server-Intel.zip"
        /usr/bin/ditto -c -k --keepParent "$APP_DIR" "$APP_ZIP" >> "$LOG" 2>&1

        log "Submitting to Apple notary service (this takes 3-10 minutes)..."
        NOTARY_OUTPUT=$(xcrun notarytool submit "$APP_ZIP" \
            --keychain-profile "$NOTARY_PROFILE" \
            --wait --timeout 30m 2>&1) || true
        echo "$NOTARY_OUTPUT" >> "$LOG"

        SUBMISSION_ID=$(echo "$NOTARY_OUTPUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)

        if echo "$NOTARY_OUTPUT" | grep -q "status: Accepted"; then
            ok ".app notarization ACCEPTED"

            # Staple
            log "Stapling .app..."
            xcrun stapler staple "$APP_DIR" >> "$LOG" 2>&1 \
                && ok ".app stapled" \
                || warn "Stapling .app failed (non-critical)"
        else
            warn ".app notarization failed or timed out"
            # Fetch log for diagnosis
            if [ -n "$SUBMISSION_ID" ]; then
                log "Fetching notary log for $SUBMISSION_ID..."
                xcrun notarytool log "$SUBMISSION_ID" \
                    --keychain-profile "$NOTARY_PROFILE" >> "$LOG" 2>&1 || true
            fi
            warn "Continuing with signed-but-unnotarized .app (users must right-click > Open)"
            result "WARN: .app notarization failed"
        fi
        rm -f "$APP_ZIP"

        # 2g. Build DMG
        header "Building Intel DMG"

        DMG_STAGE="$WORK/dmg-stage"
        rm -rf "$DMG_STAGE"
        mkdir -p "$DMG_STAGE"

        # Copy signed (and hopefully notarized+stapled) .app
        cp -R "$APP_DIR" "$DMG_STAGE/"
        ln -s /Applications "$DMG_STAGE/Applications"

        cat > "$DMG_STAGE/README.txt" <<README
Tune Server ${VERSION} — Intel

Glissez "Tune Server" dans le dossier Applications, puis lancez-le
depuis Launchpad ou /Applications.

Au lancement :
  - Le serveur demarre en arriere-plan
  - Votre navigateur s'ouvre sur http://localhost:8888
  - Pas de fenetre Terminal, pas d'icone Dock

Premier lancement : si macOS bloque "Apple ne peut verifier", faire
clic droit sur l'app puis Ouvrir -> Ouvrir dans la fenetre de
confirmation. macOS retient le choix.

Pour desinstaller : glisser l'app vers la Corbeille.
README

        DMG_PATH="$WORK/$DMG_NAME"

        if command -v create-dmg >/dev/null 2>&1; then
            log "Building DMG with create-dmg..."
            create-dmg \
                --volname "Tune Server ${VERSION}" \
                --background "$REPO_DIR/scripts/dmg_background.png" \
                --window-pos 200 120 \
                --window-size 600 400 \
                --icon-size 100 \
                --icon "Tune Server.app" 150 215 \
                --hide-extension "Tune Server.app" \
                --app-drop-link 450 215 \
                --no-internet-enable \
                "$DMG_PATH" \
                "$DMG_STAGE/" >> "$LOG" 2>&1 \
                && ok "DMG created with branded layout" \
                || {
                    warn "create-dmg failed, falling back to hdiutil"
                    hdiutil create \
                        -volname "Tune Server ${VERSION}" \
                        -srcfolder "$DMG_STAGE" \
                        -ov -format UDZO -fs HFS+ \
                        "$DMG_PATH" >> "$LOG" 2>&1 \
                        && ok "DMG created (hdiutil fallback)" \
                        || die "DMG creation failed entirely"
                }
        else
            log "create-dmg not found, using hdiutil..."
            hdiutil create \
                -volname "Tune Server ${VERSION}" \
                -srcfolder "$DMG_STAGE" \
                -ov -format UDZO -fs HFS+ \
                "$DMG_PATH" >> "$LOG" 2>&1 \
                && ok "DMG created (hdiutil)" \
                || die "DMG creation failed"
        fi

        # 2h. Sign DMG
        log "Signing DMG..."
        codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG_PATH" >> "$LOG" 2>&1 \
            && ok "DMG signed" \
            || die "DMG signing failed"

        codesign --verify --verbose=2 "$DMG_PATH" >> "$LOG" 2>&1 \
            && ok "DMG signature verified" \
            || die "DMG signature verification failed"

        # 2i. Notarize DMG
        header "Notarizing Intel DMG"
        log "Submitting DMG to Apple notary service..."
        DMG_NOTARY_OUTPUT=$(xcrun notarytool submit "$DMG_PATH" \
            --keychain-profile "$NOTARY_PROFILE" \
            --wait --timeout 30m 2>&1) || true
        echo "$DMG_NOTARY_OUTPUT" >> "$LOG"

        DMG_SUBMISSION_ID=$(echo "$DMG_NOTARY_OUTPUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)

        if echo "$DMG_NOTARY_OUTPUT" | grep -q "status: Accepted"; then
            ok "DMG notarization ACCEPTED"

            log "Stapling DMG..."
            xcrun stapler staple "$DMG_PATH" >> "$LOG" 2>&1 \
                && ok "DMG stapled" \
                || warn "DMG stapling failed"

            # Final Gatekeeper check
            if spctl --assess --type install "$DMG_PATH" >> "$LOG" 2>&1; then
                ok "DMG Gatekeeper check passed"
            else
                warn "DMG spctl check failed (may still work for users)"
            fi
        else
            warn "DMG notarization failed"
            if [ -n "$DMG_SUBMISSION_ID" ]; then
                log "Fetching notary log for DMG ($DMG_SUBMISSION_ID)..."
                xcrun notarytool log "$DMG_SUBMISSION_ID" \
                    --keychain-profile "$NOTARY_PROFILE" \
                    --output "$WORK/dmg-notary-log.json" >> "$LOG" 2>&1 || true
                if [ -f "$WORK/dmg-notary-log.json" ]; then
                    python3 -c "
import json, sys
try:
    d = json.load(open('$WORK/dmg-notary-log.json'))
    for i in d.get('issues', []):
        print(f\"  {i.get('severity','?')}: {i.get('path','?').split('/')[-1]}: {i.get('message','?')}\")
except: print('Could not parse notary log')
" 2>/dev/null | tee -a "$LOG" || true
                fi
            fi
            warn "Shipping signed-but-unnotarized DMG. Users must right-click > Open."
            result "WARN: DMG notarization failed"
        fi

        DMG_SIZE=$(du -h "$DMG_PATH" | cut -f1)
        ok "Final DMG: $DMG_NAME ($DMG_SIZE)"
        result "OK: Intel DMG built ($DMG_SIZE)"
    fi
else
    log "Skipping Intel DMG phase"
fi

# ---------------------------------------------------------------------------
# Phase 3: Upload to GitHub Release + VPS
# ---------------------------------------------------------------------------
DMG_PATH="$WORK/$DMG_NAME"

if [ "$DO_UPLOAD" = true ] && [ -f "$DMG_PATH" ]; then
    header "Phase 3: Upload"

    # 3a. Upload to GitHub Release
    if command -v gh >/dev/null 2>&1; then
        log "Uploading DMG to GitHub Release v${VERSION}..."

        # Check if the release exists
        if gh release view "v${VERSION}" --repo renesenses/tune-server-linux >/dev/null 2>&1; then
            gh release upload "v${VERSION}" "$DMG_PATH" \
                --repo renesenses/tune-server-linux \
                --clobber >> "$LOG" 2>&1 \
                && ok "GitHub Release upload" \
                || { fail "GitHub Release upload failed"; result "FAIL: GitHub upload"; }

            # Also upload SHA256
            DMG_SHA_PATH="$WORK/${DMG_NAME}.sha256"
            shasum -a 256 "$DMG_PATH" | awk '{print $1}' > "$DMG_SHA_PATH"
            gh release upload "v${VERSION}" "$DMG_SHA_PATH" \
                --repo renesenses/tune-server-linux \
                --clobber >> "$LOG" 2>&1 \
                && ok "GitHub SHA256 upload" \
                || warn "SHA256 upload failed (non-critical)"
        else
            warn "GitHub Release v${VERSION} not found — skipping upload"
            warn "Create it first: git tag v${VERSION} && git push origin v${VERSION}"
            result "SKIP: GitHub upload (release not found)"
        fi
    else
        warn "gh CLI not installed — skipping GitHub upload"
        result "SKIP: GitHub upload (gh not installed)"
    fi

    # 3b. Upload to VPS (mozaiklabs.fr)
    log "Uploading DMG to VPS..."
    VPS_RELEASE_DIR="/var/lib/docker/volumes/mozaiklabs_storage/_data/public/releases/${VERSION}"
    if ssh -o ConnectTimeout=5 "$VPS_HOST" "mkdir -p '$VPS_RELEASE_DIR'" >> "$LOG" 2>&1; then
        scp "$DMG_PATH" "${VPS_HOST}:${VPS_RELEASE_DIR}/" >> "$LOG" 2>&1 \
            && ok "VPS upload" \
            || { fail "VPS SCP failed"; result "FAIL: VPS upload"; }

        # Update ForumRelease record with Intel DMG asset
        DMG_SIZE_BYTES=$(stat -f%z "$DMG_PATH" 2>/dev/null || stat -c%s "$DMG_PATH" 2>/dev/null || echo "0")
        log "Patching ForumRelease DB for macos-intel asset..."
        ssh "$VPS_HOST" "docker exec mozaiklabs-app-1 php artisan tinker --execute=\"
            \\\$r = App\\\Models\\\ForumRelease::where('version', '${VERSION}')->first();
            if (\\\$r) {
                \\\$a = \\\$r->download_assets ?? [];
                \\\$a['macos-intel'] = [['name' => '${DMG_NAME}', 'url' => '/storage/releases/${VERSION}/${DMG_NAME}', 'size' => ${DMG_SIZE_BYTES}]];
                \\\$r->download_assets = \\\$a;
                \\\$r->save();
                echo 'ForumRelease updated';
            } else {
                echo 'ForumRelease not found for ${VERSION}';
            }
        \"" >> "$LOG" 2>&1 \
            && ok "ForumRelease DB updated" \
            || warn "ForumRelease DB update failed (update manually)"

        # Clear Laravel caches
        ssh "$VPS_HOST" "docker exec mozaiklabs-app-1 php artisan cache:clear && docker exec mozaiklabs-app-1 php artisan view:clear" >> "$LOG" 2>&1 \
            && ok "Laravel caches cleared" \
            || warn "Cache clear failed"
    else
        warn "VPS not reachable — skipping upload"
        result "SKIP: VPS upload (unreachable)"
    fi
elif [ "$DO_UPLOAD" = true ]; then
    warn "No DMG file found — skipping upload phase"
fi

# ---------------------------------------------------------------------------
# Phase 4: Cleanup + Summary
# ---------------------------------------------------------------------------
header "Cleanup"

# Clean up temp archives (keep the DMG and log)
rm -rf "$WORK/dmg-stage"
rm -f "$WORK/Tune-Server-Intel.zip"
rm -rf /tmp/Tune_iOS.xcarchive /tmp/Tune_macOS.xcarchive
rm -rf /tmp/Tune_iOS_export /tmp/Tune_macOS_export

# Keep the DMG in a convenient location
if [ -f "$DMG_PATH" ]; then
    FINAL_DMG="$REPO_DIR/$DMG_NAME"
    cp "$DMG_PATH" "$FINAL_DMG" 2>/dev/null || true
    if [ -f "$FINAL_DMG" ]; then
        ok "DMG copied to: $FINAL_DMG"
    fi
fi

ok "Temp files cleaned"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
header "Summary — Tune Server v${VERSION}"
echo "" | tee -a "$LOG"

for r in "${RESULTS[@]}"; do
    case "$r" in
        OK:*)   echo -e "  ${GREEN}✓${NC} ${r#OK: }" | tee -a "$LOG" ;;
        FAIL:*) echo -e "  ${RED}✗${NC} ${r#FAIL: }" | tee -a "$LOG" ;;
        SKIP:*) echo -e "  ${YELLOW}—${NC} ${r#SKIP: }" | tee -a "$LOG" ;;
        WARN:*) echo -e "  ${YELLOW}⚠${NC} ${r#WARN: }" | tee -a "$LOG" ;;
    esac
done

echo "" | tee -a "$LOG"
log "Full log: $LOG"

# Final DMG location
if [ -f "$REPO_DIR/$DMG_NAME" ]; then
    log "DMG: $REPO_DIR/$DMG_NAME"
fi

# Exit with error if any phase failed
for r in "${RESULTS[@]}"; do
    if [[ "$r" == FAIL:* ]]; then
        exit 1
    fi
done

echo "" | tee -a "$LOG"
echo -e "${GREEN}${BOLD}Release complete.${NC}" | tee -a "$LOG"
