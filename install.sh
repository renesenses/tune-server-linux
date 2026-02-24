#!/usr/bin/env bash
set -euo pipefail

# Tune Server installer for Debian/Ubuntu
# Usage: sudo ./install.sh [--systemd]

INSTALL_DIR="/opt/tune-server"
SERVICE_USER="tune-server"
ENABLE_SYSTEMD=false

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

# Check root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (use sudo)"
    exit 1
fi

# Check OS
if ! command -v apt-get &> /dev/null; then
    echo "Error: This installer requires apt-get (Debian/Ubuntu)"
    exit 1
fi

echo "==> Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ffmpeg \
    libasound2-dev libportaudio2 portaudio19-dev \
    avahi-daemon

echo "==> Creating service user..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
cp -r tune_server pyproject.toml "$INSTALL_DIR/"

# Create virtual environment
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"

# Create default .env if not present
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp .env.example "$INSTALL_DIR/.env"
    echo "==> Created default .env at ${INSTALL_DIR}/.env"
fi

# Set permissions
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Installation complete: ${INSTALL_DIR}"

if [[ "$ENABLE_SYSTEMD" == true ]]; then
    echo "==> Installing systemd service..."
    cp tune-server.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable tune-server
    systemctl start tune-server
    echo "==> Tune Server is running. Check status with: systemctl status tune-server"
else
    echo ""
    echo "To start manually:"
    echo "  sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python -m tune_server"
    echo ""
    echo "To install as systemd service:"
    echo "  sudo $0 --systemd"
fi
