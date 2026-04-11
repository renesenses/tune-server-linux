# Tune Server — Guide d'installation Linux

## Prérequis

- **Python 3.11+** (`python3 --version`)
- **FFmpeg** (`sudo apt install ffmpeg`)
- **Git** (`sudo apt install git`)

## Installation rapide

```bash
# Cloner le repo
git clone https://github.com/renesenses/tune-server-linux.git
cd tune-server-linux

# Créer l'environnement virtuel
python3 -m venv .venv
source .venv/bin/activate

# Installer les dépendances
pip install -e .
```

## Configuration

Créer un fichier `.env` à la racine :

```bash
cat > .env << 'EOF'
# Dossiers de musique (JSON array)
TUNE_MUSIC_DIRS='["/home/matteo/Music"]'

# Port API (défaut: 8888)
TUNE_API_PORT=8888

# Logs
TUNE_LOG_LEVEL=INFO
EOF
```

### Options supplémentaires (.env)

```bash
# Base de données (SQLite par défaut, PostgreSQL en option)
# TUNE_DB_ENGINE=postgres
# TUNE_DB_URL=postgresql://user:pass@localhost/tune

# Streaming services (optionnel)
# TUNE_DEEZER_ARL=xxx          # Cookie ARL de Deezer

# Radio France Open API (pour les podcasts)
# TUNE_RADIOFRANCE_API_KEY=xxx

# Discogs (pour les images artistes)
# TUNE_DISCOGS_TOKEN=xxx
```

## Lancement

```bash
# Activer l'environnement
source .venv/bin/activate

# Lancer le serveur
python -m tune_server
```

Le serveur démarre sur :
- **Web client** : http://localhost:8888
- **API REST** : http://localhost:8888/api/v1
- **UPnP MediaServer** : port 8080 (découverte automatique DLNA)

## Accéder au web client

Ouvrir dans un navigateur :
```
http://localhost:8888
```

Ou depuis un autre appareil sur le même réseau :
```
http://<IP-DU-PC>:8888
```

## Lancement automatique (systemd)

```bash
sudo tee /etc/systemd/system/tune-server.service << EOF
[Unit]
Description=Tune Server - Multi-room music server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python -m tune_server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable tune-server
sudo systemctl start tune-server
```

## Vérifier que ça marche

```bash
# Status du service
sudo systemctl status tune-server

# Logs en temps réel
journalctl -u tune-server -f

# Test API
curl http://localhost:8888/api/v1/system/info
```

## Mise à jour

```bash
cd tune-server-linux
git pull
source .venv/bin/activate
pip install -e .
sudo systemctl restart tune-server  # si systemd
```

## Streaming services

### Tidal
Se connecter via le web client : Paramètres → Streaming → Tidal → Se connecter

### Qobuz
Paramètres → Streaming → Qobuz → Email + mot de passe

### YouTube Music
Paramètres → Streaming → YouTube → Authentification OAuth

### Deezer
Nécessite le cookie ARL dans le `.env` :
1. Se connecter sur deezer.com dans le navigateur
2. Ouvrir les DevTools (F12) → Application → Cookies → `arl`
3. Copier la valeur dans `.env` : `TUNE_DEEZER_ARL=xxx`

## Dépannage

| Problème | Solution |
|----------|----------|
| Port 8888 occupé | `TUNE_API_PORT=9999` dans `.env` |
| Pas de son local | `sudo apt install portaudio19-dev` puis `pip install sounddevice` |
| FFmpeg manquant | `sudo apt install ffmpeg` |
| Permission dossier musique | `chmod -R 755 /chemin/vers/musique` |
