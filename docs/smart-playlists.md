# Smart Playlists

Playlists dynamiques qui se mettent a jour automatiquement selon des regles de filtrage.

## API

### Lister les smart playlists
```
GET /api/v1/library/smart-playlists
```

### Creer une smart playlist
```
POST /api/v1/library/smart-playlists
Content-Type: application/json

{
  "name": "Jazz 1959",
  "description": "Les classiques du jazz de 1959",
  "rules": [
    { "field": "genre", "operator": "contains", "value": "Jazz" },
    { "field": "year", "operator": "equals", "value": "1959" }
  ],
  "match_mode": "all",
  "sort_by": "album",
  "sort_order": "asc",
  "max_tracks": 200
}
```

### Recuperer les pistes d'une smart playlist
```
GET /api/v1/library/smart-playlists/{id}/tracks
```

### Modifier une smart playlist
```
PUT /api/v1/library/smart-playlists/{id}
Content-Type: application/json

{
  "name": "Jazz 1959 (updated)",
  "rules": [...],
  "max_tracks": 100
}
```

### Supprimer une smart playlist
```
DELETE /api/v1/library/smart-playlists/{id}
```

## Champs disponibles

| Champ | Description | Exemple |
|-------|-------------|---------|
| `title` | Titre de la piste | "Blue in Green" |
| `artist` | Nom de l'artiste | "Miles Davis" |
| `album` | Titre de l'album | "Kind of Blue" |
| `genre` | Genre | "Jazz" |
| `year` | Annee de l'album | "1959" |
| `format` | Format audio | "flac", "aac", "mp3", "dsd" |
| `sample_rate` | Frequence d'echantillonnage (Hz) | "96000", "192000" |
| `bit_depth` | Profondeur de bits | "24", "32" |
| `source` | Source | "local", "tidal", "qobuz" |
| `composer` | Compositeur | "Cole Porter" |

## Operateurs

| Operateur | Description | Applicable a |
|-----------|-------------|-------------|
| `contains` | Contient (insensible a la casse) | Texte |
| `equals` | Egal exactement | Texte, Nombre |
| `not_equals` | Different de | Texte, Nombre |
| `starts_with` | Commence par | Texte |
| `greater_than` | Superieur a | Nombre (year, sample_rate, bit_depth) |
| `less_than` | Inferieur a | Nombre |

## Mode de correspondance

- `"all"` (defaut) : toutes les regles doivent correspondre (ET logique)
- `"any"` : au moins une regle doit correspondre (OU logique)

## Tri

| sort_by | Description |
|---------|-------------|
| `title` | Par titre (defaut) |
| `artist` | Par artiste |
| `album` | Par album |
| `year` | Par annee |
| `duration` | Par duree |
| `track_number` | Par numero de piste |
| `random` | Aleatoire |

`sort_order` : `"asc"` (defaut) ou `"desc"`

## Exemples

### Jazz enregistre apres 1958
```json
{
  "name": "Jazz post-1958",
  "rules": [
    { "field": "genre", "operator": "contains", "value": "Jazz" },
    { "field": "year", "operator": "greater_than", "value": "1958" }
  ],
  "match_mode": "all",
  "sort_by": "year",
  "sort_order": "asc"
}
```

### Hi-Res uniquement (> 48 kHz)
```json
{
  "name": "Hi-Res Collection",
  "rules": [
    { "field": "sample_rate", "operator": "greater_than", "value": "48000" }
  ],
  "sort_by": "artist"
}
```

### Pat Metheny aleatoire (max 50)
```json
{
  "name": "Pat Metheny Shuffle",
  "rules": [
    { "field": "artist", "operator": "contains", "value": "Pat Metheny" }
  ],
  "sort_by": "random",
  "max_tracks": 50
}
```

### FLAC ou DSD uniquement
```json
{
  "name": "Lossless Only",
  "rules": [
    { "field": "format", "operator": "equals", "value": "flac" },
    { "field": "format", "operator": "equals", "value": "dsd" }
  ],
  "match_mode": "any"
}
```

### Compositions de Cole Porter
```json
{
  "name": "Cole Porter Songbook",
  "rules": [
    { "field": "composer", "operator": "contains", "value": "Cole Porter" }
  ],
  "sort_by": "album",
  "sort_order": "asc"
}
```

### Albums Tidal recents
```json
{
  "name": "Mes enregistrements Tidal",
  "rules": [
    { "field": "source", "operator": "equals", "value": "tidal" }
  ],
  "sort_by": "year",
  "sort_order": "desc",
  "max_tracks": 500
}
```
