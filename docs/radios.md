# Radios — Tune Server

Liste de référence des radios configurées, avec URLs de streaming et sources des logos.
Utilisable pour recréer les radios en cas de reset de la base de données.

## Stations

Flux Radio France : Icecast AAC 192 kbps (meilleure qualité disponible).

| # | Nom | Genre | URL de streaming | Logo |
|---|-----|-------|-----------------|------|
| 1 | FIP | Éclectique | `https://icecast.radiofrance.fr/fip-hifi.aac` | `radio-logos/fip.png` |
| 2 | FIP Jazz | Jazz | `https://icecast.radiofrance.fr/fipjazz-hifi.aac` | `radio-logos/fip-jazz.jpeg` |
| 3 | FIP Electro | Electro | `https://icecast.radiofrance.fr/fipelectro-hifi.aac` | `radio-logos/fip-electro.jpeg` |
| 4 | FIP Monde | World | `https://icecast.radiofrance.fr/fipworld-hifi.aac` | `radio-logos/fip-monde.jpeg` |
| 5 | FIP Rock | Rock | `https://icecast.radiofrance.fr/fiprock-hifi.aac` | `radio-logos/fip-rock.jpeg` |
| 6 | FIP Groove | Groove | `https://icecast.radiofrance.fr/fipgroove-hifi.aac` | `radio-logos/fip-groove.jpeg` |
| 7 | FIP Pop | Pop | `https://icecast.radiofrance.fr/fippop-hifi.aac` | `radio-logos/fip-pop.jpeg` |
| 8 | FIP Reggae | Reggae | `https://icecast.radiofrance.fr/fipreggae-hifi.aac` | `radio-logos/fip-reggae.jpeg` |
| 9 | FIP Nouveautés | Nouveautés | `https://icecast.radiofrance.fr/fipnouveautes-hifi.aac` | `radio-logos/fip-nouveau.jpeg` |
| 10 | FIP Metal | Metal | `https://icecast.radiofrance.fr/fipmetal-hifi.aac` | `radio-logos/fip-metal.jpeg` |
| 11 | France Inter | Généraliste | `https://icecast.radiofrance.fr/franceinter-hifi.aac` | `radio-logos/france_inter.png` |
| 12 | France Culture | Culture | `https://icecast.radiofrance.fr/franceculture-hifi.aac` | `radio-logos/france_culture.png` |
| 13 | France Musique | Classique | `https://icecast.radiofrance.fr/francemusique-hifi.aac` | `radio-logos/france_musique.png` |
| 14 | Radio Classique | Classique | `https://radioclassique.ice.infomaniak.ch/radioclassique-high.mp3` | `radio-logos/radio_classique.png` |

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

## Logos

Tous les logos sont dans `docs/radio-logos/`.

- **FIP thématiques** : visuels distinctifs avec typographie bold et couleurs vives (exportés depuis Roon)
- **FIP, France Inter, France Culture, France Musique** : logos officiels 500x500 PNG (Wikimedia Commons 2021)
- **Radio Classique** : logo officiel 500x500 PNG (Wikimedia Commons)

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
