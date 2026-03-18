# TUNE Server v0.1.5 — Release Notes

## Nouveautés

### Audio & Appareils
- **Contrôle du volume Micromega M-One** : intégration du protocole propriétaire pour le contrôle natif du volume
- **DSD natif sur Micromega M-One** : détection et activation automatique du passthrough DSD
- **Proxy HTTPS→HTTP** : proxy transparent pour les flux Tidal/Qobuz sur les renderers DLNA sans support HTTPS
- **Fallback radio HTTPS→HTTP** : les flux radio HTTPS sont automatiquement convertis pour les renderers sans TLS

### Bibliothèque & API
- **Écriture des tags** : `PUT /library/tracks/{id}` et `PUT /library/albums/{id}` écrivent les métadonnées directement dans les fichiers audio (FLAC, MP3, M4A, OGG)
- **Création d'artistes** : nouvel endpoint `POST /library/artists`
- **Gestion des dossiers musique à chaud** : ajout/suppression de répertoires via API sans redémarrage
- **PATCH pour les zones** : mise à jour partielle de la configuration des zones

### Multi-room
- **Synchronisation améliorée** : polling adaptatif (1s actif / 10s idle), requêtes de position sur les sorties
- **Offset par zone** : champ `sync_delay_ms` pour un réglage fin de la synchronisation multi-room
- **Latence DLNA adaptative** : mesure et cache de la latence réelle des renderers au lieu d'un délai fixe de 3s

### Web Client
- **Logo Tune** dans la sidebar
- **Indicateur de lecture** : barres d'equalizer animées sur l'album en cours dans le carousel
- **Artistes cliquables** pour les sources streaming/radio

## Corrections
- **Buffer non aligné** provoquant le skip de tous les morceaux
- **Chemins Windows** : normalisation backslash→slash
- **Un appareil par zone** : empêche l'assignation d'un même appareil à plusieurs zones
- **Source ID artistes streaming** : ajout du source_id aux réponses artistes Qobuz/Tidal
- **Pagination playlists Qobuz** : les playlists de plus de 50 morceaux récupèrent tous les éléments
- **Version dynamique** : lecture depuis pyproject.toml au lieu d'une valeur hardcodée

## Téléchargement

Rendez-vous sur [mozaiklabs.fr/download](https://mozaiklabs.fr/download) ou directement sur GitHub :
- [Linux (tar.gz)](https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5)
- [macOS Apple Silicon (tar.gz)](https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5)
- [macOS Intel (tar.gz)](https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5)
- [Windows (zip)](https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5)
