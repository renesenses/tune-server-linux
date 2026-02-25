# Installing Ubuntu Server on a Mac Mini Late 2012

Guide for setting up Ubuntu Server on a Mac Mini Late 2012 as a dedicated tune-server host.

## Hardware Specs (Mac Mini Late 2012)

- CPU: Intel Core i5-3210M or i7-3615QM (Ivy Bridge, 64-bit)
- RAM: 4 GB or 16 GB DDR3 (upgradeable to 16 GB)
- Storage: HDD 500 GB / 1 TB or Fusion Drive (SSD replaceable)
- Audio: Built-in 3.5mm headphone/optical out (TOS-Link combo)
- Network: Gigabit Ethernet + Wi-Fi 802.11n
- Boot: EFI 64-bit (no legacy BIOS)

## Recommended Ubuntu Version

**Ubuntu Server 24.04 LTS** (Noble Numbat) — supported until 2029.

Ubuntu 22.04 LTS works too, but 24.04 has better kernel support for Ivy Bridge power management.

## Step 1 — Prepare the USB Installer

On your Mac (or any machine):

```bash
# Download Ubuntu Server 24.04 LTS
curl -LO https://releases.ubuntu.com/24.04/ubuntu-24.04-live-server-amd64.iso

# Identify the USB drive (BE CAREFUL with the disk number)
diskutil list

# Unmount the USB drive (replace diskN with the correct disk)
diskutil unmountDisk /dev/diskN

# Write the ISO to USB (use rdiskN for faster writes)
sudo dd if=ubuntu-24.04-live-server-amd64.iso of=/dev/rdiskN bs=4m status=progress

# Eject
diskutil eject /dev/diskN
```

Alternative: use [balenaEtcher](https://etcher.balena.io/) for a GUI approach.

## Step 2 — Boot from USB

1. Plug the USB drive into the Mac Mini
2. Power on (or restart) while holding the **Option (⌥)** key
3. The boot picker appears — select **EFI Boot** (the orange/yellow USB icon)
4. Select **Try or Install Ubuntu Server**

> **Note:** If the USB doesn't appear, try a USB 2.0 port (the ones closest to the HDMI port).

## Step 3 — Install Ubuntu Server

Follow the installer prompts:

1. **Language**: English (or your preference)
2. **Keyboard**: French / French (Macintosh) if applicable
3. **Network**: The Ethernet interface should auto-configure via DHCP. Note the IP for SSH later.
4. **Storage layout** — select **Custom storage layout**, then create:

   | # | Partition | Size | Type | Mount | Rôle |
   |---|-----------|------|------|-------|------|
   | 1 | `/dev/sda1` | 512 MB | EFI System Partition (fat32) | `/boot/efi` | Bootloader Mac EFI |
   | 2 | `/dev/sda2` | 1 GB | ext4 | `/boot` | Kernel & initrd |
   | 3 | `/dev/sda3` | 48.5 GB | ext4 | `/` | Système + programmes (~50 Go total avec boot) |
   | 4 | `/dev/sda4` | 950 GB | ext4 | `/mnt/music` | Bibliothèque musicale |

   > **Pas de swap ?** Avec 8 Go+ de RAM pour un serveur audio, le swap n'est pas nécessaire. Ubuntu crée un fichier swap automatique (`/swap.img`) si besoin.

   Dans l'installeur Ubuntu :
   1. Sélectionner le SSD (`/dev/sda` — ~1 TB)
   2. **"Add GPT Partition"** pour chaque partition ci-dessus
   3. Pour la partition EFI : choisir **Format: fat32**, **Mount: /boot/efi**
   4. Pour les autres : choisir **Format: ext4** et le point de montage correspondant
   5. Vérifier le récapitulatif, puis **Done** → **Continue**
5. **Profile**:
   - Name / username / password as desired
   - Server name suggestion: `tune-server` or `mac-mini`
6. **SSH**: **Enable OpenSSH server** — essential for headless operation
7. **Featured snaps**: Skip (we'll install tune-server via .deb)

The installer will finish and ask to reboot. Remove the USB drive when prompted.

## Step 4 — First Boot & Initial Setup

### Connect via SSH

```bash
ssh your-username@<ip-address>
```

### Update the system

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

### Set a static IP (optional but recommended for a server)

Edit the Netplan config:

```bash
sudo nano /etc/netplan/50-cloud-init.yaml
```

```yaml
network:
  version: 2
  ethernets:
    eno1:
      dhcp4: no
      addresses:
        - 192.168.1.50/24
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses:
          - 1.1.1.1
          - 8.8.8.8
```

```bash
sudo netplan apply
```

### Set the timezone

```bash
sudo timedatectl set-timezone Europe/Paris
```

## Step 5 — Install tune-server

### Option A: From .deb package

```bash
# Transfer the .deb to the Mac Mini
scp tune-server_0.1.0-1_all.deb your-username@192.168.1.50:~

# On the Mac Mini:
sudo apt install -y ./tune-server_0.1.0-1_all.deb
```

This will:
- Install all dependencies (ffmpeg, portaudio, avahi, python3, etc.)
- Create the `tune-server` system user
- Install to `/opt/tune-server` with a Python venv
- Enable the systemd service

### Option B: From source with install.sh

```bash
git clone https://github.com/renesenses/tune-server.git
cd tune-server
sudo ./install.sh
```

### Configure

```bash
sudo nano /opt/tune-server/.env
```

Key settings to configure:

```bash
# Point to your music library
TUNE_MUSIC_DIRS=["/mnt/music"]

# Network
TUNE_API_PORT=8888
TUNE_STREAM_PORT=8080
```

### Start the service

```bash
sudo systemctl start tune-server
sudo systemctl status tune-server

# View logs
sudo journalctl -u tune-server -f
```

## Step 6 — Audio Output (Headphone/Optical)

The Mac Mini Late 2012 has a combo 3.5mm jack that supports both analog and optical (TOS-Link) output.

```bash
# List audio devices
aplay -l

# Test audio
speaker-test -c 2 -t wav

# If the tune-server user needs audio access
sudo usermod -aG audio tune-server
sudo systemctl restart tune-server
```

For optical output, insert a mini-TOSLINK cable — the port switches automatically.

## Step 7 — Music Storage

La partition `/mnt/music` (950 Go, `/dev/sda4`) est déjà montée automatiquement via le partitionnement fait à l'installation.

```bash
# Vérifier le montage
df -h /mnt/music

# Donner les droits au user tune-server
sudo chown tune-server:tune-server /mnt/music
sudo chmod 755 /mnt/music
```

Configurer tune-server pour utiliser cette partition :

```bash
# Dans /opt/tune-server/.env :
TUNE_MUSIC_DIRS=["/mnt/music"]
```

### Transférer de la musique

```bash
# Depuis votre Mac, via SCP
scp -r ~/Music/* your-username@192.168.1.50:/mnt/music/

# Ou via rsync (reprend les transferts interrompus)
rsync -avz --progress ~/Music/ your-username@192.168.1.50:/mnt/music/
```

### Stockage supplémentaire (NFS, optionnel)

Si vous avez aussi un NAS :

```bash
sudo apt install -y nfs-common
sudo mkdir -p /mnt/nas-music
echo 'nas.local:/volume1/music  /mnt/nas-music  nfs  defaults,ro,soft  0  0' | sudo tee -a /etc/fstab
sudo mount -a

# Ajouter les deux répertoires dans .env :
TUNE_MUSIC_DIRS=["/mnt/music", "/mnt/nas-music"]
```

## Step 8 — Firewall

```bash
sudo ufw allow ssh
sudo ufw allow 8888/tcp comment "Tune Server API"
sudo ufw allow 8080/tcp comment "Tune Server audio stream"
sudo ufw allow 1900/udp comment "SSDP discovery"
sudo ufw allow 5353/udp comment "mDNS/Avahi"
sudo ufw enable
```

## Step 9 — Power Management

Prevent the Mac Mini from sleeping:

```bash
# Disable all sleep/suspend
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

### Auto power-on after power loss

This is set in the Mac's firmware. Before installing Ubuntu, from macOS:

```bash
sudo pmset autorestart 1
```

If macOS is already gone, you can set this from Ubuntu using `setpci` (Ivy Bridge specific):

```bash
# Check current value
sudo setpci -s 0:1f.0 0xa4.b

# Set bit 0 to enable auto power-on (value depends on current reading)
# Example: if current value is 0x00, set to 0x01
sudo setpci -s 0:1f.0 0xa4.b=01
```

> **Tip:** A smart plug with scheduled power-on is a simpler alternative.

## Step 10 — Wi-Fi (Optional)

The built-in Broadcom Wi-Fi may need a proprietary driver:

```bash
sudo apt install -y bcmwl-kernel-source
```

However, for a music server, **wired Ethernet is strongly recommended** for reliability and latency.

## Troubleshooting

### No boot from USB

- Hold **Option (⌥)** immediately after the startup chime
- Try different USB ports (prefer USB 2.0)
- Re-flash the USB with balenaEtcher

### No network after install

```bash
# Check interface name
ip link show

# If interface is down
sudo ip link set eno1 up
sudo dhclient eno1
```

### Screen goes blank / no display

Ubuntu Server is headless by default after boot. Connect via SSH. If you need a console:

```bash
# Edit GRUB to remove "quiet splash"
sudo nano /etc/default/grub
# Set: GRUB_CMDLINE_LINUX_DEFAULT=""
sudo update-grub
```

### Fan running loud

Install `mbpfan` for Apple-specific fan control:

```bash
sudo apt install -y mbpfan
sudo systemctl enable --now mbpfan
```
