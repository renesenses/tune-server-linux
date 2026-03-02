# Tune — Notes de développement

## Calendrier

- **Début du projet** : 24 février 2026
- **Date actuelle** : 2 mars 2026
- **Durée de développement** : 7 jours

## Codebase

### Backend (tune-server) — Python / FastAPI

| Métrique | Valeur |
|----------|--------|
| Fichiers Python | 107 |
| Lignes de code | 20 983 |
| Classes | 61 |
| Fonctions / méthodes | 1 438 |
| dont fonctions async | 926 (64%) |
| Endpoints API REST | 106 |
| Commits | 63 |
| Dépendances | 26 |

### Frontend (tune-web-client) — Svelte 5 / TypeScript

| Métrique | Valeur |
|----------|--------|
| Composants Svelte | 23 |
| Fichiers TypeScript | 28 |
| Lignes de code | 16 432 |
| dont i18n (8 langues) | 2 302 |
| Commits | 61 |

### Documentation

| Métrique | Valeur |
|----------|--------|
| Fichiers Markdown | ~15 |
| Lignes de documentation | 6 061 |

### Total projet

| | |
|---|---|
| **Lignes de code totales** | **~37 400** |
| **Commits totaux** | **124** |

## Architecture

- **Backend** : Python 3.11+, FastAPI, SQLite (aiosqlite), structlog
- **Frontend** : Svelte 5, TypeScript, Vite
- **Communication** : REST API + WebSocket (événements temps réel)
- **Audio** : FFmpeg (transcodage), yt-dlp (YouTube), passthrough DSD natif

## Services de streaming intégrés

| Service | Auth | Streaming complet |
|---------|------|-------------------|
| Tidal | OAuth device code | Oui (HiRes) |
| Qobuz | Username/password | Oui (HiRes) |
| YouTube Music | Google OAuth device code | Oui (via yt-dlp) |
| Amazon Music | OAuth device code | Oui (SD/HD/Ultra HD) |
| Spotify | OAuth PKCE | Non (previews 30s) |
| Deezer | OAuth 2.0 standard | Non (previews 30s) |

## Sorties audio

- DLNA/UPnP (avec détection capabilities par device)
- AirPlay (via pyatv)
- Sortie locale (USB DAC, HDMI)
- Multi-room avec grouping de zones

## Fonctionnalités clés développées en 7 jours

- Scan et indexation de bibliothèque musicale locale
- Lecture multi-room (DLNA, AirPlay, local)
- 5 connecteurs de streaming (Tidal, Qobuz, YouTube Music, Spotify, Deezer)
- DSD/DSF natif passthrough + transcodage WAV 176.4kHz
- Recherche fédérée (local + tous services)
- Interface web responsive en 8 langues
- File d'attente avec drag & drop
- Playlists, radios internet (CRUD, import M3U/PLS)
- Navigation par répertoire et par métadonnées
- Dashboard maintenance (covers, genres, années manquants)
- Découverte réseau (SSDP, mDNS, partages SMB/NFS)
- API REST complète (106 endpoints) + WebSocket temps réel
- Système de backup/restore SQLite
