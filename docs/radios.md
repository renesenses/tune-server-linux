# Radios — Tune Server

Liste de référence des radios configurées, avec URLs de streaming et sources des logos.
Utilisable pour recréer les radios en cas de reset de la base de données.

## Stations

Flux Radio France : Icecast AAC 192 kbps (meilleure qualité disponible).

| # | Nom | Genre | URL de streaming | Logo source |
|---|-----|-------|-----------------|-------------|
| 1 | FIP | Éclectique | `https://icecast.radiofrance.fr/fip-hifi.aac` | Wikimedia: FIP_logo_2021.svg |
| 2 | FIP Jazz | Jazz | `https://icecast.radiofrance.fr/fipjazz-hifi.aac` | (logo FIP) |
| 3 | FIP Electro | Electro | `https://icecast.radiofrance.fr/fipelectro-hifi.aac` | (logo FIP) |
| 4 | FIP Monde | World | `https://icecast.radiofrance.fr/fipworld-hifi.aac` | (logo FIP) |
| 5 | FIP Rock | Rock | `https://icecast.radiofrance.fr/fiprock-hifi.aac` | (logo FIP) |
| 6 | FIP Groove | Groove | `https://icecast.radiofrance.fr/fipgroove-hifi.aac` | (logo FIP) |
| 7 | FIP Pop | Pop | `https://icecast.radiofrance.fr/fippop-hifi.aac` | (logo FIP) |
| 8 | FIP Reggae | Reggae | `https://icecast.radiofrance.fr/fipreggae-hifi.aac` | (logo FIP) |
| 9 | FIP Nouveautés | Nouveautés | `https://icecast.radiofrance.fr/fipnouveautes-hifi.aac` | (logo FIP) |
| 10 | FIP Metal | Metal | `https://icecast.radiofrance.fr/fipmetal-hifi.aac` | (logo FIP) |
| 11 | France Inter | Généraliste | `https://icecast.radiofrance.fr/franceinter-hifi.aac` | Wikimedia: France_Inter_logo_2021.svg |
| 12 | France Culture | Culture | `https://icecast.radiofrance.fr/franceculture-hifi.aac` | Wikimedia: France_Culture_logo_2021.svg |
| 13 | France Musique | Classique | `https://icecast.radiofrance.fr/francemusique-hifi.aac` | Wikimedia: France_Musique_logo_2021.svg |
| 14 | Radio Classique | Classique | `https://radioclassique.ice.infomaniak.ch/radioclassique-high.mp3` | Wikimedia: Logo_Radio_Classique.svg |

Toutes les stations sont marquées comme favorites.

## Qualités disponibles (Radio France)

| Qualité | Format | Suffixe URL |
|---------|--------|-------------|
| 192 kbps | AAC | `-hifi.aac` |
| 128 kbps | MP3 | `-midfi.mp3` |
| 96 kbps | AAC | `-midfi.aac` |
| 32 kbps | AAC | `-lofi.aac` |
| 32 kbps | MP3 | `-lofi.mp3` |
| HLS | M3U8 | via `stream.radiofrance.fr` |

Base URL : `https://icecast.radiofrance.fr/`

## Sources des logos (Wikimedia Commons, 500x500 PNG)

Téléchargement via l'API Wikipedia (les téléchargements directs Wikimedia sont bloqués) :

```bash
# Récupérer l'URL du thumbnail via l'API
curl -s "https://en.wikipedia.org/w/api.php?action=query&titles=File:FIP_logo_2021.svg&prop=imageinfo&iiprop=url&iiurlwidth=500&format=json"
```

| Fichier Wikimedia | Utilisé pour |
|-------------------|-------------|
| `FIP_logo_2021.svg` | FIP + toutes les thématiques FIP |
| `France_Inter_logo_2021.svg` | France Inter |
| `France_Culture_logo_2021.svg` | France Culture |
| `France_Musique_logo_2021.svg` | France Musique |
| `Logo_Radio_Classique.svg` | Radio Classique |

## Recréation rapide (curl)

```bash
# Créer une station
curl -X POST http://localhost:8888/api/v1/radios \
  -H 'Content-Type: application/json' \
  -d '{"name":"FIP","stream_url":"https://icecast.radiofrance.fr/fip-hifi.aac","genre":"Éclectique","favorite":true}'

# Uploader le logo
curl -X POST http://localhost:8888/api/v1/radios/1/artwork \
  -F "file=@fip.png"
```
