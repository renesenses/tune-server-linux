#!/bin/bash
# Tune Server — macOS installer
# Usage:  curl -fsSL https://mozaiklabs.fr/install-macos.sh | bash
#         # or, for a specific version:
#         curl -fsSL https://mozaiklabs.fr/install-macos.sh | bash -s -- 0.7.20
#
# What it does:
#   1. Detects ARM vs Intel
#   2. Downloads the matching Python server bundle from mozaiklabs.fr
#   3. Installs to ~/Applications/Tune Server/
#   4. Sets up launchd LaunchAgent so it starts at login + auto-restart
#   5. Starts it once
#
# Tune.app (the SwiftUI client) is distributed via TestFlight separately:
# https://testflight.apple.com — the user joins as a tester. The app talks to
# the local Python server on http://localhost:8888.

set -euo pipefail

VERSION="${1:-latest}"
INSTALL_DIR="$HOME/Applications/Tune Server"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LAUNCHD_PLIST="$LAUNCHD_DIR/com.mozaiklabs.tune-server.plist"
LOG_DIR="$HOME/Library/Logs/Tune"

# ANSI colors for nice output
B="\033[1m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"

echo -e "${B}Tune Server — macOS installer${N}"
echo

# 1. Detect architecture
ARCH="$(uname -m)"
case "$ARCH" in
    arm64)
        ASSET_SUFFIX="macos.tar.gz"
        echo -e "  Architecture : ${G}Apple Silicon (arm64)${N}"
        ;;
    x86_64)
        ASSET_SUFFIX="macos-intel.tar.gz"
        echo -e "  Architecture : ${G}Intel (x86_64)${N}"
        ;;
    *)
        echo -e "  ${R}Architecture inattendue: $ARCH${N}"
        exit 1
        ;;
esac

# 2. Resolve version
if [ "$VERSION" = "latest" ]; then
    echo -n "  Resolution de la derniere version… "
    VERSION="$(curl -fsSL https://mozaiklabs.fr/api/v1/updates.json \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["latest"]["version"])')"
    echo -e "${G}v$VERSION${N}"
fi

URL="https://mozaiklabs.fr/api/v1/releases/tune-server-${VERSION}-${ASSET_SUFFIX}"
echo -e "  URL          : $URL"

# 3. Download + extract
TMP_DIR="$(mktemp -d)"
trap "rm -rf '$TMP_DIR'" EXIT

echo
echo -n "  Telechargement… "
curl -fsSL -o "$TMP_DIR/tune-server.tar.gz" "$URL"
echo -e "${G}OK${N}"

echo -n "  Extraction… "
tar -xzf "$TMP_DIR/tune-server.tar.gz" -C "$TMP_DIR"
echo -e "${G}OK${N}"

# 4. Install to ~/Applications/Tune Server
mkdir -p "$INSTALL_DIR" "$LAUNCHD_DIR" "$LOG_DIR"

# Stop running service before overwriting
if [ -f "$LAUNCHD_PLIST" ]; then
    echo -n "  Arret du service existant… "
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    echo -e "${G}OK${N}"
fi

# Preserve user data: tune_server.db, .env, artwork_cache
DATA_BACKUP=""
if [ -d "$INSTALL_DIR/tune-server" ]; then
    DATA_BACKUP="$(mktemp -d)"
    for f in tune_server.db tune_server.db-shm tune_server.db-wal .env; do
        if [ -f "$INSTALL_DIR/tune-server/$f" ]; then
            cp -p "$INSTALL_DIR/tune-server/$f" "$DATA_BACKUP/"
        fi
    done
    if [ -d "$INSTALL_DIR/tune-server/artwork_cache" ]; then
        cp -rp "$INSTALL_DIR/tune-server/artwork_cache" "$DATA_BACKUP/"
    fi
    rm -rf "$INSTALL_DIR/tune-server"
fi

echo -n "  Installation dans ${INSTALL_DIR}… "
mv "$TMP_DIR/tune-server" "$INSTALL_DIR/"
echo -e "${G}OK${N}"

# Restore user data
if [ -n "$DATA_BACKUP" ]; then
    echo -n "  Restauration de la base utilisateur… "
    for f in tune_server.db tune_server.db-shm tune_server.db-wal .env; do
        if [ -f "$DATA_BACKUP/$f" ]; then
            mv "$DATA_BACKUP/$f" "$INSTALL_DIR/tune-server/"
        fi
    done
    if [ -d "$DATA_BACKUP/artwork_cache" ]; then
        mv "$DATA_BACKUP/artwork_cache" "$INSTALL_DIR/tune-server/"
    fi
    rm -rf "$DATA_BACKUP"
    echo -e "${G}OK${N}"
fi

# Make the binary executable + remove quarantine attribute (otherwise
# Gatekeeper will block since the binary isn't notarized).
chmod +x "$INSTALL_DIR/tune-server/tune-server"
xattr -dr com.apple.quarantine "$INSTALL_DIR/tune-server" 2>/dev/null || true

# 5. Write launchd plist
cat > "$LAUNCHD_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mozaiklabs.tune-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/tune-server/tune-server</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}/tune-server</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/tune-server.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/tune-server.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

echo -n "  LaunchAgent installe… "
launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
launchctl load "$LAUNCHD_PLIST"
echo -e "${G}OK${N}"

# 6. Smoke check
echo -n "  Verification HTTP /api/v1/system/health… "
sleep 4
if curl -fsS -o /dev/null --max-time 5 http://localhost:8888/api/v1/system/health 2>/dev/null; then
    echo -e "${G}OK${N}"
else
    echo -e "${Y}pas encore disponible (le serveur peut prendre 10-30s a demarrer apres un cold start)${N}"
fi

echo
echo -e "${B}Installation terminee.${N}"
echo "  Interface web    : http://localhost:8888"
echo "  Logs             : ${LOG_DIR}/tune-server.log"
echo "  Mise a jour      : relancez ce script (vos donnees sont preservees)"
echo "  Desinstallation  : launchctl unload ${LAUNCHD_PLIST} && rm -rf '${INSTALL_DIR}' '${LAUNCHD_PLIST}'"
echo
echo "  L'app native Tune (iOS / iPadOS / macOS) est sur TestFlight :"
echo "  https://testflight.apple.com/join/XXXX  (lien sur demande)"
echo "  Configurez l'app en mode Remote sur localhost:8888."
