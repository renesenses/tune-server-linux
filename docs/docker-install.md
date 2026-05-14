# Installation de Tune Server via Docker

Guide pour installer Tune Server sur n'importe quelle machine Linux via Docker — NAS (Synology, QNAP, Unraid), Raspberry Pi, VPS, PC.

## Prérequis

- Docker Engine 20+ et Docker Compose v2
- Accès réseau local (DLNA/AirPlay nécessitent le mode `host`)
- Dossier musique accessible sur la machine hôte

## Installation rapide

### 1. Créer le dossier Tune

```bash
mkdir -p ~/tune-server/data
cd ~/tune-server
```

### 2. Créer le fichier docker-compose.yml

```yaml
services:
  tune:
    image: renesenses/tune:latest
    container_name: tune-server
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./data:/data
      - /chemin/vers/votre/musique:/music:ro
    environment:
      - TUNE_MUSIC_DIRS=["/music"]
      - TUNE_DB_PATH=/data/tune_server.db
      - TUNE_ARTWORK_CACHE_DIR=/data/artwork_cache
      - TUNE_API_PORT=8888
      - TUNE_LOG_LEVEL=INFO
```

> **Important** : `network_mode: host` est obligatoire pour la découverte DLNA (SSDP multicast), AirPlay (mDNS) et Chromecast. Sans ce mode, Tune ne détectera pas vos appareils réseau.

### 3. Adapter le chemin musique

Remplacez `/chemin/vers/votre/musique` par le chemin réel de votre bibliothèque :

| Plateforme | Exemple |
|------------|---------|
| Linux / Raspberry Pi | `/home/pi/Music` |
| Synology | `/volume1/music` |
| QNAP | `/share/Music` |
| Unraid | `/mnt/user/music` |
| Montage SMB | `/mnt/nas/music` (voir section NAS ci-dessous) |

Plusieurs dossiers musique :
```yaml
    volumes:
      - ./data:/data
      - /volume1/music/flac:/music/flac:ro
      - /volume1/music/hires:/music/hires:ro
    environment:
      - TUNE_MUSIC_DIRS=["/music/flac", "/music/hires"]
```

### 4. Lancer

```bash
docker compose up -d
```

### 5. Accéder à l'interface

Ouvrez votre navigateur : `http://adresse-ip:8888`

L'interface web permet de :
- Parcourir et lire votre bibliothèque
- Configurer les zones de lecture (local, DLNA, AirPlay, Chromecast)
- Connecter vos services streaming (Qobuz, Tidal, Deezer, Spotify)
- Gérer les playlists et collections intelligentes

## Configuration avancée

### PostgreSQL (recommandé pour les grosses bibliothèques)

Par défaut, Tune utilise SQLite. Pour les bibliothèques de plus de 50 000 pistes, PostgreSQL est recommandé :

```yaml
services:
  tune:
    image: renesenses/tune:latest
    container_name: tune-server
    restart: unless-stopped
    network_mode: host
    depends_on:
      db:
        condition: service_healthy
    volumes:
      - ./data:/data
      - /chemin/vers/votre/musique:/music:ro
    environment:
      - TUNE_MUSIC_DIRS=["/music"]
      - TUNE_DB_ENGINE=postgres
      - TUNE_DB_URL=postgresql://tune:tune@127.0.0.1:5432/tune
      - TUNE_ARTWORK_CACHE_DIR=/data/artwork_cache
      - TUNE_API_PORT=8888

  db:
    image: postgres:16-alpine
    container_name: tune-db
    restart: unless-stopped
    volumes:
      - ./pgdata:/var/lib/postgresql/data
    environment:
      POSTGRES_USER: tune
      POSTGRES_PASSWORD: tune
      POSTGRES_DB: tune
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tune"]
      interval: 5s
      timeout: 3s
      retries: 5
```

### Services streaming

Ajoutez vos tokens dans les variables d'environnement :

```yaml
    environment:
      - TUNE_LASTFM_API_KEY=votre_clé
      - TUNE_LASTFM_API_SECRET=votre_secret
      - TUNE_DISCOGS_TOKEN=votre_token
```

Qobuz, Tidal, Deezer et Spotify se configurent depuis l'interface web (Paramètres > Services streaming).

### Sécurité (clé API)

Pour protéger l'accès à l'API :

```yaml
    environment:
      - TUNE_API_KEY=votre_clé_secrète
```

### Audio bit-perfect (sortie USB locale)

Pour utiliser un DAC USB branché directement sur la machine hôte :

```yaml
    volumes:
      - ./data:/data
      - /chemin/vers/votre/musique:/music:ro
    devices:
      - /dev/snd:/dev/snd
    environment:
      - TUNE_LOCAL_EXCLUSIVE_MODE=true
```

### Crossfade

```yaml
    environment:
      - TUNE_CROSSFADE_ENABLED=true
      - TUNE_CROSSFADE_DURATION=3.0
```

## Installation sur NAS spécifiques

### Synology (DSM 7+)

1. Installez le paquet **Container Manager** depuis le Centre de paquets
2. Créez un dossier partagé `docker/tune-server`
3. Dans Container Manager > Projet > Créer :
   - Nom : `tune-server`
   - Chemin : `/volume1/docker/tune-server`
   - Collez le docker-compose.yml ci-dessus
   - Adaptez le chemin musique : `/volume1/music:/music:ro`
4. Construire et démarrer

> **Note** : Sur Synology, `network_mode: host` fonctionne mais le pare-feu intégré peut bloquer les ports. Allez dans Panneau de configuration > Sécurité > Pare-feu et autorisez les ports 8888 et 8080.

### QNAP (Container Station)

1. Installez **Container Station** depuis l'App Center
2. Allez dans Container Station > Compose > Créer
3. Collez le docker-compose.yml
4. Adaptez : `/share/Music:/music:ro`
5. Démarrer

### Unraid

1. Allez dans l'onglet Docker
2. Ajoutez un nouveau container via le template ou Compose
3. Chemin musique : `/mnt/user/music:/music:ro`
4. Network Type : `Host`

## Accès depuis un NAS distant (montage SMB)

Si votre musique est sur un NAS distant et Tune tourne sur une autre machine (Raspberry Pi, PC) :

```bash
# Installer cifs-utils
sudo apt install cifs-utils

# Créer le point de montage
sudo mkdir -p /mnt/nas/music

# Montage manuel (test)
sudo mount -t cifs //192.168.1.100/music /mnt/nas/music -o username=votre_user,password=votre_mdp,ro,uid=1000

# Montage automatique au boot (/etc/fstab)
echo '//192.168.1.100/music /mnt/nas/music cifs username=votre_user,password=votre_mdp,ro,uid=1000,_netdev 0 0' | sudo tee -a /etc/fstab
```

Puis dans docker-compose.yml :
```yaml
    volumes:
      - /mnt/nas/music:/music:ro
```

## Commandes utiles

```bash
# Voir les logs
docker logs -f tune-server

# Redémarrer
docker compose restart

# Mettre à jour
docker compose pull && docker compose up -d

# Arrêter
docker compose down

# Voir l'utilisation des ressources
docker stats tune-server
```

## Mise à jour

```bash
cd ~/tune-server
docker compose pull
docker compose up -d
```

Les données (bibliothèque, playlists, zones) sont persistées dans `./data` et ne sont pas affectées par la mise à jour.

## Dépannage

### Tune ne détecte pas mes appareils DLNA/AirPlay

Vérifiez que `network_mode: host` est bien configuré. En mode bridge (par défaut), les paquets multicast SSDP et mDNS ne traversent pas le réseau Docker.

### Port 8888 déjà utilisé

```yaml
    environment:
      - TUNE_API_PORT=9999
```

### Permission denied sur le dossier musique

```bash
# Vérifier que Docker a accès au dossier
ls -la /chemin/vers/votre/musique

# Si besoin, ajuster les permissions
chmod -R o+r /chemin/vers/votre/musique
```

### Erreur FFmpeg not found

FFmpeg est inclus dans l'image Docker. Si vous voyez cette erreur, l'image est peut-être corrompue :

```bash
docker compose pull --force
docker compose up -d
```
