# Roon vs Tune — Comparaison des fonctionnalités

## Vue d'ensemble

| | **Roon** | **Tune** |
|---|---|---|
| **Type** | Logiciel propriétaire commercial | Projet open source |
| **Prix** | ~830€ à vie ou ~15€/mois | Gratuit |
| **Licence** | Propriétaire (closed source) | Open source |
| **Architecture** | Roon Core + Remote(s) | FastAPI backend + Svelte SPA |
| **Langages** | C# / .NET (backend), propriétaire | Python (backend), TypeScript/Svelte (frontend) |
| **Plateformes serveur** | Windows, macOS, Linux, Roon OS (ROCK/Nucleus) | Toute machine avec Python 3.11+ |
| **Clients** | Apps natives (iOS, Android, Windows, macOS) | Navigateur web (responsive) |

## Bibliothèque musicale

| Fonctionnalité | **Roon** | **Tune** |
|---|---|---|
| Scan bibliothèque locale | Oui | Oui |
| Formats supportés | FLAC, WAV, AIFF, DSD, MP3, AAC, ALAC, OGG, etc. | FLAC, WAV, AIFF, DSD, MP3, AAC, ALAC, OGG, OPUS, WMA |
| Métadonnées automatiques | Oui (Roon DB, AllMusic, MusicBrainz, TiVo) | Oui (MusicBrainz, Discogs) |
| Édition des métadonnées | Oui (éditeur intégré riche) | Basique (réassignation album/artiste) |
| Gestion des doublons | Oui (identification avancée) | Non |
| Pochettes d'albums | Oui (multi-sources automatiques) | Oui (scan automatique, upload manuel) |
| Biographies artistes | Oui (AllMusic, intégrées dans l'UI) | Non (champ bio prévu) |
| Lyrics | Oui (synchronisés) | Non |
| Credits / Line-up | Oui (musiciens, producteurs, ingénieurs) | Non |
| Navigation par répertoire | Non (navigation par métadonnées uniquement) | Oui (browse par dossiers) |
| Playlists locales | Oui | Oui |

## Services de streaming

| Service | **Roon** | **Tune** |
|---|---|---|
| Tidal | Oui (streaming complet HiRes) | Oui (streaming complet HiRes) |
| Qobuz | Oui (streaming complet HiRes) | Oui (streaming complet HiRes) |
| YouTube Music | Non | Oui (streaming complet via yt-dlp) |
| Spotify | Non | Oui (navigation, previews 30s) |
| Deezer | Non | Oui (navigation, previews 30s) |
| Amazon Music | Non | Oui (streaming complet, OAuth device code) |
| Radio internet | Oui (Live Radio, TuneIn) | Oui (CRUD, import M3U/PLS) |
| Recherche fédérée | Oui (Tidal + Qobuz + local) | Oui (tous services + local) |

## Lecture et sorties audio

| Fonctionnalité | **Roon** | **Tune** |
|---|---|---|
| Multi-room | Oui (Roon Ready, AirPlay, Chromecast, DLNA) | Oui (DLNA, AirPlay) |
| Grouping de zones | Oui (lecture synchronisée) | Oui |
| Lecteur local (USB/HDMI) | Oui (ASIO, WASAPI, ALSA, CoreAudio) | Oui (sounddevice) |
| DLNA/UPnP | Oui | Oui (avec détection capabilities par device) |
| AirPlay | Oui | Oui (via pyatv) |
| Chromecast | Oui | Non |
| Roon Ready (RAAT) | Oui (protocole propriétaire, basse latence) | Non |
| Squeezebox | Oui | Non |
| DSD/DSF natif | Oui (DoP et natif) | Oui (passthrough natif DSF/DFF vers renderers compatibles, détection heuristique par device) |
| Transcodage DSD | Oui | Oui (DSD → WAV 176.4kHz/24bit, famille 44.1kHz respectée) |
| Upsampling | Oui (paramétrable, HQPlayer) | Non |
| DSP / Égaliseur | Oui (paramétrique, convolution, crossfeed) | Non |
| Gapless | Oui | Dépend du renderer |
| Normalisation volume | Oui (ReplayGain, analyse intégrée) | Non |
| Bit-perfect | Oui (vérifié, signal path visible) | Oui (passthrough FLAC/WAV/DSD) |
| Signal path | Oui (visualisation complète du traitement audio) | Non |

## Découverte réseau

| Fonctionnalité | **Roon** | **Tune** |
|---|---|---|
| SSDP (DLNA renderers) | Oui | Oui |
| mDNS/Bonjour (AirPlay) | Oui | Oui |
| Partages réseau SMB/NFS | Oui (intégré) | Oui (montage + scan) |
| Serveurs média DLNA | Non (Roon est son propre serveur) | Oui (browse contenu) |

## Interface utilisateur

| Fonctionnalité | **Roon** | **Tune** |
|---|---|---|
| Design | Premium, très soigné | Sobre, fonctionnel |
| Thème sombre/clair | Oui | Oui |
| Multi-langues | Oui (~10 langues) | Oui (8 langues) |
| Now Playing | Oui (pochette grand format, lyrics, credits) | Oui (pochette, barre de progression) |
| Queue / File d'attente | Oui | Oui (drag & drop réorganisation) |
| Recherche globale | Oui (très rapide, tous contenus) | Oui (fédérée local + streaming) |
| Vue albums | Oui (grille, filtres, tri) | Oui (grille/liste, tri) |
| Vue artistes | Oui (photos, discographie, bio) | Oui (grille, discographie) |
| Vue genres | Oui | Oui (avec sous-vue albums/tracks) |
| Historique d'écoute | Oui | Oui |
| Maintenance / complétude | Non (édition manuelle) | Oui (dashboard : covers, genres, années manquants) |
| Raccourcis clavier | Limité | Non |
| Interface mobile native | Oui (iOS, Android) | Non (web responsive) |

## Architecture et déploiement

| Aspect | **Roon** | **Tune** |
|---|---|---|
| Installation | Installeur graphique | `pip install` ou git clone |
| Configuration | GUI dans l'app | Fichier `.env` + interface web |
| Base de données | Propriétaire (fichiers internes) | SQLite |
| API | Non documentée / privée | REST API complète + WebSocket |
| Extensibilité | Extensions Roon (API limitée) | Open source, modifiable |
| Ressources (RAM) | 4-8 Go minimum | ~200 Mo |
| Matériel dédié | Recommandé (Nucleus, NUC) | N'importe quel serveur |
| Sauvegarde/restauration | Oui (intégrée) | Oui (backup SQLite) |

## Points forts respectifs

### Roon
- Métadonnées exceptionnelles (credits, lyrics, bios, liens entre artistes)
- DSP intégré puissant (EQ paramétrique, convolution, room correction)
- Signal path transparent
- Protocol RAAT propriétaire basse latence
- Interface premium et apps natives
- Intégration HQPlayer
- Écosystème Roon Ready (certifié par les fabricants)

### Tune
- Gratuit et open source
- Plus de services de streaming (6 : Tidal, Qobuz, YouTube Music, Amazon Music, Spotify, Deezer vs 2 pour Roon)
- DSD/DSF natif passthrough vers renderers compatibles (détection automatique par device)
- API REST complète et documentée
- Léger en ressources
- Navigation par répertoire (en plus des métadonnées)
- Dashboard de maintenance de bibliothèque
- Pas de dépendance cloud (fonctionne 100% en local)
- Extensible et modifiable
- Pas d'abonnement ni de licence

## Conclusion

**Roon** excelle dans l'expérience audiophile haut de gamme : métadonnées riches, DSP avancé, signal path transparent, et un écosystème certifié. C'est le choix pour qui veut une solution "tout-en-un" premium et est prêt à payer.

**Tune** est une alternative open source légère qui offre plus de flexibilité : plus de services de streaming, une API ouverte, et une empreinte serveur minimale. C'est le choix pour qui veut le contrôle total, sans abonnement, avec la possibilité de contribuer et d'étendre le logiciel.
