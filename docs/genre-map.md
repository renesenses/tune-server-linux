# Cartographie des Genres — Tune Library (.18)

*Mise à jour : 2026-04-14 — après sync Roon + MusicBrainz*

## Genres actuels (47 genres distincts)

| Genre | Albums | Notes |
|-------|--------|-------|
| **Pop-Rock** | 532 | Genre principal |
| **Jazz Classic** | 171 | Jazz traditionnel / standards |
| **Electro** | 120 | Musique électronique |
| **Chanson Francaise** | 114 | Chanson française contemporaine |
| **Jazz** | 91 | Jazz générique |
| **Jazz Europe** | 87 | Jazz européen |
| **Jazz US** | 79 | Jazz américain |
| **Rock** | 78 | Rock classique |
| **Compilation** | 74 | Compilations |
| **Chanson Francaise Classique** | 66 | Chanson française classique (Gainsbourg, Nougaro...) |
| **Variétés Internationales** | 51 | Pop internationale |
| **CLASSICAL** | 49 | Classique (majuscules) |
| **JAZZ** | 40 | Jazz (majuscules — doublon) |
| **Classical** | 23 | Classique |
| **Blues** | 15 | Blues |
| **Bossa Nova** | 13 | Bossa nova |
| **Other** | 13 | Non classé |
| **R&B-Soul** | 12 | R&B et Soul |
| **World Music** | 12 | Musiques du monde |
| **International** | 11 | International |
| **Pop/Rock** | 11 | Pop/Rock (variante) |
| **Pop Rock** | 7 | Pop Rock (variante) |
| **Soundtrack** | 10 | Bandes originales |
| **Pop** | 10 | Pop |
| **Jazz Electro** | 8 | Jazz électronique |
| **Jazz Manouche** | 8 | Jazz manouche |
| **Divers** | 4 | Divers |
| **R&B** | 3 | R&B |
| **Electronic** | 2 | Electronique (doublon Electro) |
| **Progressive Rock** | 2 | Rock progressif |
| **Reggae** | 2 | Reggae |
| **Acoustic** | 1 | |
| **Chanson** | 1 | |
| **Disco** | 1 | |
| **Indie** | 1 | |
| **Latin** | 1 | |
| **opera** | 1 | |
| **Rap** | 1 | |
| **Rock Music** | 1 | |
| **Soft Rock** | 1 | |
| **Stage & Screen** | 1 | |
| **World Pop** | 1 | |

### Anomalies à corriger

| Problème | Action |
|----------|--------|
| `JAZZ` (40) vs `Jazz` (91) | Normaliser en `Jazz` |
| `CLASSICAL` (49) vs `Classical` (23) | Normaliser en `Classical` |
| `Pop-Rock` (532), `Pop/Rock` (11), `Pop Rock` (7) | Normaliser en `Pop-Rock` |
| `Electronic` (2) vs `Electro` (120) | Normaliser en `Electro` |
| `Rock Music` (1), `Soft Rock` (1) | → `Rock` |
| `Chanson` (1) | → `Chanson Francaise` |
| `0` (1), `26053061` (1), `Friend-Rip` (1), `Platinum Shm-Cd` (1), `Limited Edition` (1) | Erreurs de tagging — supprimer |

### Hiérarchie proposée (après normalisation)

```
Pop-Rock (550+)
├── Pop
├── Rock
├── Progressive Rock
├── Indie
└── Soft Rock

Jazz (300+)
├── Jazz Classic
├── Jazz US
├── Jazz Europe
├── Jazz Manouche
├── Jazz Electro
└── Bossa Nova

Chanson Francaise (180+)
├── Chanson Francaise
└── Chanson Francaise Classique

Electro (122+)
└── Electronic

Classical (72)
├── Classical
└── Opera

Blues (15)

R&B-Soul (15)
├── R&B
└── Soul

World Music (13+)
├── International
├── Latin
├── Reggae
└── World Pop

Soundtrack (10+)
├── Soundtrack
└── Stage & Screen

Compilation (74)

Other / Divers (17)
```
