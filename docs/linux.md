# Linux Deployment Guide

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
