# Linux Deployment Guide

## Installation via Debian Package

The recommended way to deploy Tune Server on Debian/Ubuntu is the `.deb` package.

### Build the package

```bash
# Prerequisites
sudo apt install build-essential debhelper python3 python3-venv python3-pip

# Build (from the tune-server repo root)
./build-deb.sh
```

The package is created in the parent directory (`../tune-server_*.deb`).

### Install

```bash
sudo dpkg -i tune-server_0.1.0_all.deb
sudo apt install -f  # resolve any missing dependencies
```

This installs:
- Application to `/opt/tune-server/` (source + virtualenv + web UI)
- systemd service `tune-server.service`
- Configuration at `/opt/tune-server/.env`
- System user `tune-server` (no login shell)

### Post-install configuration

```bash
# Edit configuration
sudo nano /opt/tune-server/.env

# Start the service
sudo systemctl start tune-server

# Check logs
sudo journalctl -u tune-server -f
```

### Upgrade

```bash
sudo dpkg -i tune-server_<new-version>.deb
```

The `.env` file is preserved across upgrades (declared as conffile). The service restarts automatically via `postinst`.

### Uninstall

```bash
sudo apt remove tune-server         # keep config and data
sudo apt purge tune-server           # remove everything (config, data, user)
```

---

## Audio Configuration

### ALSA (direct hardware access)

ALSA is the default audio backend on Debian/Ubuntu. Tune Server uses `sounddevice` (which wraps PortAudio) for local audio output.

```bash
# Install ALSA development libraries
sudo apt install libasound2-dev

# List available audio devices
aplay -l

# Test audio output
speaker-test -c 2 -t wav
```

### PulseAudio

If PulseAudio is running, PortAudio will use it automatically. No additional configuration is needed.

```bash
# Check PulseAudio status
pulseaudio --check && echo "running" || echo "not running"

# List sinks
pactl list short sinks
```

### PipeWire

PipeWire provides PulseAudio and ALSA compatibility layers. Tune Server works with PipeWire out of the box.

```bash
# Check PipeWire status
systemctl --user status pipewire

# List devices
pw-cli list-objects | grep -i audio
```

## mDNS / Avahi (AirPlay Discovery)

Tune Server uses mDNS (via Avahi) to discover AirPlay devices on the network.

```bash
# Install and enable Avahi
sudo apt install avahi-daemon avahi-utils
sudo systemctl enable --now avahi-daemon

# Verify Avahi is running
avahi-browse -a -t

# Browse for AirPlay devices specifically
avahi-browse -t _raop._tcp
avahi-browse -t _airplay._tcp
```

If Avahi is not running, AirPlay device discovery will be unavailable but DLNA/UPnP and local output will still work.

## Firewall (ufw)

If `ufw` is enabled, open the required ports:

```bash
# REST API
sudo ufw allow 8888/tcp comment "Tune Server API"

# HTTP audio streaming (DLNA)
sudo ufw allow 8080/tcp comment "Tune Server audio stream"

# SSDP (DLNA discovery)
sudo ufw allow 1900/udp comment "SSDP discovery"

# mDNS (AirPlay discovery)
sudo ufw allow 5353/udp comment "mDNS/Avahi"

# Verify
sudo ufw status verbose
```

## Network Mounts (SMB/NFS)

To mount network shares, the `tune-server` user needs permission to run `mount`/`umount`.

### Configure sudoers

```bash
# Create sudoers rule (no password required for mount/umount)
sudo visudo -f /etc/sudoers.d/tune-server
```

Add:
```
tune-server ALL=(ALL) NOPASSWD: /usr/bin/mount, /usr/bin/umount
```

### Enable network share discovery

In `/opt/tune-server/.env`:
```bash
TUNE_NETWORK_SHARES_ENABLED=true
TUNE_SMB_MOUNT_DIR=/mnt/tune-shares
```

Create the mount directory:
```bash
sudo mkdir -p /mnt/tune-shares
sudo chown tune-server:tune-server /mnt/tune-shares
```

### Install SMB client (for SMB shares)

```bash
sudo apt install cifs-utils
```

### Install NFS client (for NFS exports)

```bash
sudo apt install nfs-common
```

Mounted shares are automatically added to the music directories and scanned by the library.

---

## Running as a systemd Service

```bash
# Copy the service file
sudo cp tune-server.service /etc/systemd/system/

# Create the service user (if not using install.sh)
sudo useradd --system --create-home --shell /usr/sbin/nologin tune-server

# Reload systemd and enable
sudo systemctl daemon-reload
sudo systemctl enable --now tune-server

# Check status
sudo systemctl status tune-server

# View logs
sudo journalctl -u tune-server -f

# Restart after config changes
sudo systemctl restart tune-server
```

### Granting access to music directories

The systemd unit runs with `ProtectHome=read-only`. If your music is under `/home`, it is accessible read-only by default. For music on external drives:

```bash
# Example: mount point /mnt/music
# Add to the [Service] section of tune-server.service:
ReadOnlyPaths=/mnt/music
```

Then reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart tune-server
```

### systemd overrides

To customize the service without editing the unit file (survives package upgrades):

```bash
sudo systemctl edit tune-server
```

Common overrides:

```ini
[Service]
# Allow access to external mount points
ReadWritePaths=/mnt/music /mnt/tune-shares

# Increase restart delay
RestartSec=10

# Set environment variables directly
Environment="TUNE_LOG_LEVEL=DEBUG"
```

Then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart tune-server
```

---

## Troubleshooting

### No audio output

1. Check that PortAudio sees your audio devices:
   ```bash
   python3 -c "import sounddevice; print(sounddevice.query_devices())"
   ```

2. If running as a systemd service, the service user may not have access to the audio group:
   ```bash
   sudo usermod -aG audio tune-server
   sudo systemctl restart tune-server
   ```

### DLNA devices not discovered

1. Check that SSDP multicast traffic is allowed:
   ```bash
   sudo ufw allow 1900/udp
   ```

2. Verify the server and DLNA devices are on the same subnet.

3. Check firewall/iptables rules that might block multicast.

### AirPlay devices not discovered

1. Ensure Avahi is running:
   ```bash
   sudo systemctl status avahi-daemon
   ```

2. Check that mDNS port is open:
   ```bash
   sudo ufw allow 5353/udp
   ```

3. Verify AirPlay devices are visible:
   ```bash
   avahi-browse -t _airplay._tcp
   ```

### FFmpeg not found

```bash
sudo apt install ffmpeg
ffmpeg -version
```

### Database locked errors

If you see `database is locked` errors, ensure only one instance of Tune Server is running:

```bash
ps aux | grep tune_server
```

### Permission denied on music files

Ensure the service user can read the music directories:

```bash
sudo -u tune-server ls /path/to/music/
```

For external mount points, add them to the systemd override:
```bash
sudo systemctl edit tune-server
# Add: ReadWritePaths=/mnt/music
```

### Network mount failures

1. Check sudoers is configured:
   ```bash
   sudo -u tune-server sudo -n mount --version
   # Should not ask for password
   ```

2. Check SMB/NFS client is installed:
   ```bash
   dpkg -l | grep cifs-utils    # for SMB
   dpkg -l | grep nfs-common    # for NFS
   ```

3. Check mount directory exists and is writable:
   ```bash
   ls -la /mnt/tune-shares/
   ```

### Streaming services not connecting

1. Check the service is enabled and configured in `.env`:
   ```bash
   grep -E 'TIDAL|QOBUZ|YOUTUBE|AMAZON' /opt/tune-server/.env
   ```

2. Check authentication status:
   ```bash
   curl -s localhost:8888/api/v1/streaming/services | python3 -m json.tool
   ```

3. For Tidal/YouTube/Amazon (OAuth): the authentication flow requires a browser. Use the API to initiate:
   ```bash
   curl -s -X POST localhost:8888/api/v1/streaming/tidal/auth | python3 -m json.tool
   # Follow the verification_url in the response
   ```

4. For Qobuz: provide credentials:
   ```bash
   curl -s -X POST localhost:8888/api/v1/streaming/qobuz/auth \
     -H 'Content-Type: application/json' \
     -d '{"username": "user@email.com", "password": "pass"}'
   ```

### Web UI not loading

1. Check `TUNE_WEB_DIR` is set and points to the built files:
   ```bash
   grep WEB_DIR /opt/tune-server/.env
   ls /opt/tune-server/web/index.html
   ```

2. Ensure the directory contains `index.html` and `assets/` at the root (not inside a `dist/` subdirectory).
