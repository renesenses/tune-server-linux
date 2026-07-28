#!/usr/bin/env bash
set -euo pipefail

# Tune Server installer for Linux (Debian/Ubuntu, Fedora/RHEL, Arch)
# Detects whether the script runs from a PyInstaller binary tarball
# (the GitHub release artifact, default since v0.7.x) or from a source
# checkout, and installs accordingly.
#
# Usage: sudo ./install.sh [--systemd]

INSTALL_DIR="/opt/tune-server"
SERVICE_USER="tune-server"
ENABLE_SYSTEMD=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
    case "$arg" in
        --systemd) ENABLE_SYSTEMD=true ;;
        --help|-h)
            echo "Usage: sudo $0 [--systemd]"
            echo ""
            echo "Options:"
            echo "  --systemd    Install and enable systemd service"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            exit 1
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (use sudo)"
    exit 1
fi

# ---------------------------------------------------------------------------
# DEPRECATED — the Python edition of Tune Server is no longer maintained.
#
# Its HTTP API has fallen behind the current Tune web/mobile clients: a fresh
# install serves a UI that calls routes this backend never implemented, so the
# user gets 404 errors, a wrong "FREE" licence display, and empty log
# downloads. The current, actively developed server is tune-server-rust —
# install that instead. Refuse by default; a documented escape hatch
# (TUNE_ALLOW_DEPRECATED=1) still lets a determined user proceed.
# ---------------------------------------------------------------------------
if [[ "${TUNE_ALLOW_DEPRECATED:-0}" != "1" ]]; then
    cat >&2 <<'EOF'

============================================================================
  Tune Server — Python edition — DEPRECATED / NO LONGER MAINTAINED
============================================================================

  This installer ships an old server whose API no longer matches the Tune
  web and mobile clients. Installing it leads to 404 errors, a wrong "FREE"
  licence display, and empty log downloads.

  Install the current server instead — tune-server-rust:

    * Docker (recommended on Linux):
        docker run -d --name tune-server --network host \
          -v /path/to/music:/music:ro -v tune-data:/data \
          -e TUNE_AUTO_SCAN=true renesenses/tune:dev

    * Download page (binaries, macOS DMG, Windows):
        https://mozaiklabs.fr/download

    * GitHub releases:
        https://github.com/renesenses/tune-server-rust/releases

  (Advanced) To force this legacy Python install anyway:
        TUNE_ALLOW_DEPRECATED=1 sudo ./install.sh [--systemd]

============================================================================

EOF
    exit 1
fi

echo "==> WARNING: installing the DEPRECATED Python edition (TUNE_ALLOW_DEPRECATED=1 set)."
echo "==> The current server is tune-server-rust — https://mozaiklabs.fr/download"

# Detect package manager
PKG_MANAGER=""
if command -v apt-get &> /dev/null; then
    PKG_MANAGER="apt"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v pacman &> /dev/null; then
    PKG_MANAGER="pacman"
else
    echo "Error: No supported package manager found."
    echo "  This installer supports: apt (Debian/Ubuntu), dnf (Fedora/RHEL), pacman (Arch)"
    exit 1
fi
echo "==> Detected package manager: $PKG_MANAGER"

# Detect mode: PyInstaller binary tarball ships a `tune-server` Mach-O/ELF
# in the same directory plus an `_internal/` folder. Source checkout has
# `tune_server/` and `pyproject.toml` instead.
if [[ -x "$SCRIPT_DIR/tune-server" && -d "$SCRIPT_DIR/_internal" ]]; then
    MODE="binary"
elif [[ -d "$SCRIPT_DIR/tune_server" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    MODE="source"
else
    echo "Error: Could not determine install mode."
    echo "  Expected either:"
    echo "    - PyInstaller bundle: tune-server + _internal/ in $SCRIPT_DIR"
    echo "    - Source checkout: tune_server/ + pyproject.toml in $SCRIPT_DIR"
    exit 1
fi
echo "==> Detected install mode: $MODE"

# Binary mode portability check: the PyInstaller bundle ships its own
# libstdc++.so.6, but newer wheels (numpy ≥ 2.1, Pillow ≥ 11) require
# GLIBCXX_3.4.32+ which the build runner's libstdc++ doesn't carry. We
# pinned those wheels in v0.7.55 to manylinux_2_17 baseline to avoid the
# drift, but old binaries from earlier releases will still crash on
# Ubuntu < 22.04 / CentOS 7 / Debian 11. Fail-fast with a clear hint.
if [[ "$MODE" == "binary" ]]; then
    _ldconfig=$(command -v ldconfig || echo /sbin/ldconfig)
    if ! "$_ldconfig" -p 2>/dev/null | grep -q libstdc++.so.6 && ! find /usr/lib* /lib* -name "libstdc++.so.6" -print -quit 2>/dev/null | grep -q .; then
        echo "Error: libstdc++.so.6 missing on this system."
        echo "  apt install libstdc++6       (Debian/Ubuntu)"
        echo "  dnf install libstdc++        (Fedora/RHEL)"
        echo "  pacman -S gcc-libs           (Arch)"
        echo "  or use the source install: git clone + pip install -e ."
        exit 1
    fi
fi

echo "==> Installing system dependencies..."
if [[ "$PKG_MANAGER" == "apt" ]]; then
    apt-get update -qq
fi

install_packages() {
    case "$PKG_MANAGER" in
        apt)    apt-get install -y -qq "$@" ;;
        dnf)    dnf install -y -q "$@" ;;
        pacman) pacman -S --noconfirm --needed "$@" ;;
    esac
}

if [[ "$MODE" == "source" ]]; then
    # Source mode needs Python toolchain to build the venv.
    case "$PKG_MANAGER" in
        apt)
            install_packages python3 python3-pip python3-venv ffmpeg curl \
                libasound2-dev libportaudio2 portaudio19-dev \
                avahi-daemon
            ;;
        dnf)
            install_packages python3 python3-pip python3-devel ffmpeg curl \
                alsa-lib-devel portaudio portaudio-devel \
                avahi
            ;;
        pacman)
            install_packages python python-pip ffmpeg curl \
                alsa-lib portaudio \
                avahi
            ;;
    esac
else
    # Binary mode: PyInstaller bundle ships its own Python + dyn libs.
    # Only ffmpeg + portaudio runtime + avahi remain as runtime deps.
    case "$PKG_MANAGER" in
        apt)
            install_packages ffmpeg curl rsync libportaudio2 avahi-daemon
            ;;
        dnf)
            install_packages ffmpeg curl portaudio avahi
            ;;
        pacman)
            install_packages ffmpeg curl portaudio avahi
            ;;
    esac
fi

echo "==> Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# Stop any existing service before overwriting files.
if systemctl is-active --quiet tune-server 2>/dev/null; then
    echo "==> Stopping running tune-server service..."
    systemctl stop tune-server
fi
# Also kill any tune-server still bound to ports (e.g. user ran the binary
# manually outside systemd). Otherwise a stale 0.7.x process keeps the port
# and the freshly installed version can't bind.
pkill -f "$INSTALL_DIR/tune-server" 2>/dev/null || true
fuser -k 8888/tcp 2>/dev/null || true
sleep 1

echo "==> Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"

if [[ "$MODE" == "binary" ]]; then
    # Replace the bundle in-place. Preserve user data dirs (/opt/tune-server/.env,
    # tune_server.db, artwork_cache, etc.) if they already exist.
    rsync -a --delete \
        --exclude '.env' \
        --exclude 'tune_server.db*' \
        --exclude 'artwork_cache' \
        --exclude 'logs' \
        "$SCRIPT_DIR/" "$INSTALL_DIR/"
    # Make the launcher symlink-friendly.
    chmod +x "$INSTALL_DIR/tune-server" "$INSTALL_DIR/librespot" 2>/dev/null || true
else
    # Source: copy code + (re)build venv.
    cp -r "$SCRIPT_DIR/tune_server" "$SCRIPT_DIR/pyproject.toml" "$INSTALL_DIR/"
    [[ -d "$SCRIPT_DIR/web" ]] && cp -r "$SCRIPT_DIR/web" "$INSTALL_DIR/"
    python3 -m venv "$INSTALL_DIR/.venv"
    "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"
fi

# Create default .env if not present.
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
        cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env"
        echo "==> Created default .env at ${INSTALL_DIR}/.env"
    else
        : > "$INSTALL_DIR/.env"
    fi
fi

# Drop any TUNE_WEB_DIR override left over from older installs.
# In binary mode the bundle keeps its assets at _internal/web/ and the
# server auto-detects that path. A stale TUNE_WEB_DIR=/opt/tune-server/web
# (the old layout) makes the UI 404 because that folder doesn't exist
# in PyInstaller bundles. (Reported by Matteo on Ubuntu after upgrading.)
if [[ "$MODE" == "binary" && -f "$INSTALL_DIR/.env" ]]; then
    if grep -qE '^TUNE_WEB_DIR=' "$INSTALL_DIR/.env"; then
        sed -i.bak '/^TUNE_WEB_DIR=/d' "$INSTALL_DIR/.env"
        echo "==> Removed stale TUNE_WEB_DIR from .env (binary mode auto-detects)"
    fi
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Installation complete: ${INSTALL_DIR}"

# Show installed version so the user can verify the swap actually happened.
INSTALLED_VERSION=""
if [[ "$MODE" == "binary" ]]; then
    INSTALLED_VERSION=$(ls "$INSTALL_DIR/_internal/" 2>/dev/null \
        | sed -n 's/^tune_server-\(.*\)\.dist-info$/\1/p' | head -n1)
elif [[ -f "$INSTALL_DIR/pyproject.toml" ]]; then
    INSTALLED_VERSION=$(grep -E '^version = ' "$INSTALL_DIR/pyproject.toml" \
        | head -n1 | sed -E 's/version = "(.*)"/\1/')
fi
[[ -n "$INSTALLED_VERSION" ]] && echo "==> Installed version: $INSTALLED_VERSION"

if [[ "$ENABLE_SYSTEMD" == true ]]; then
    echo "==> Installing systemd service..."
    if [[ -f "$SCRIPT_DIR/tune-server.service" ]]; then
        cp "$SCRIPT_DIR/tune-server.service" /etc/systemd/system/
    fi
    # Patch ExecStart for the install mode. The tune-server.service file
    # ships with the source-mode command (.venv/bin/python -m tune_server)
    # because that's still the shape for git-checkout dev installs. In
    # binary mode the .venv doesn't exist — systemd would fail with
    # 203/EXEC every first start. Rewrite ExecStart to point at the
    # PyInstaller binary. (Reported by Matteo on Ubuntu.)
    if [[ "$MODE" == "binary" ]]; then
        sed -i.bak 's|^ExecStart=.*$|ExecStart=/opt/tune-server/tune-server|' \
            /etc/systemd/system/tune-server.service
        # Restart=always (not on-failure) so /system/restart actually relaunches
        # the process. /system/restart sends SIGTERM, which is a clean exit
        # — on-failure wouldn't catch it.
        sed -i.bak 's|^Restart=on-failure$|Restart=always|' \
            /etc/systemd/system/tune-server.service
    fi
    systemctl daemon-reload
    systemctl enable tune-server
    systemctl restart tune-server
    sleep 2
    systemctl status tune-server --no-pager | head -10 || true
else
    echo ""
    if [[ "$MODE" == "binary" ]]; then
        echo "To start manually:"
        echo "  sudo -u $SERVICE_USER $INSTALL_DIR/tune-server"
    else
        echo "To start manually:"
        echo "  sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python -m tune_server"
    fi
    echo ""
    echo "To install as systemd service:"
    echo "  sudo $0 --systemd"
fi
