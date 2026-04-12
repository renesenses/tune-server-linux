# Tune — Cas d'usage du Gestionnaire de Playlists

## 1. Transfert Tidal → Local (backup)

**Scénario** : Sauvegarder une playlist Tidal en local, au cas où l'abonnement expire.

```mermaid
sequenceDiagram
    participant U as Utilisateur
    participant T as Tune
    participant TI as Tidal
    participant LIB as Bibliothèque locale

    U->>T: Long-press "Jazz Favs" → Transférer
    T->>TI: Charger 50 tracks
    loop Matching par track
        T->>LIB: Recherche ISRC / titre+artiste
        LIB-->>T: Match trouvé (FLAC)
    end
    T-->>U: 45 matchés, 3 approximatifs, 2 non trouvés
    U->>T: Valider
    T->>T: Créer playlist locale "Jazz Favs"
```

**Résultat** : Playlist locale avec les fichiers FLAC de ta bibliothèque, indépendante de Tidal.

---

## 2. Migration Tidal → Qobuz (batch)

**Scénario** : Changement d'abonnement — retrouver toutes ses playlists sur Qobuz.

```mermaid
graph LR
    T[Tidal<br>280 playlists] -->|Batch Transfer| PM[Playlist Manager]
    PM -->|Matching ISRC| Q[Qobuz]
    PM -->|Rapport| R[260 OK<br>15 partielles<br>5 échouées]
    
    style PM fill:#7574F3,color:#fff
    style R fill:#10B981,color:#fff
```

**Comment** :
1. Gestionnaire → onglet **Backup** → section "Batch Transfer"
2. Source: Tidal → Cible: Qobuz
3. Toutes les playlists transférées avec matching ISRC
4. Rapport consultable dans l'onglet **Transferts**

---

## 3. Fusion de playlists cross-services

**Scénario** : Combiner "Road Trip" (Tidal) + "Driving" (Deezer) en une seule playlist.

```mermaid
graph TD
    P1["Road Trip<br>Tidal — 35 tracks"] --> M{Merge +<br>Déduplicate}
    P2["Driving<br>Deezer — 28 tracks"] --> M
    M --> R["Road Trip Mix<br>Local — 52 tracks uniques"]
    
    style M fill:#7574F3,color:#fff
    style R fill:#10B981,color:#fff
```

**Résultat** : Les doublons (même titre + artiste) sont automatiquement supprimés. Les tracks sont matchées à la bibliothèque locale quand possible.

---

## 4. Synchronisation continue

**Scénario** : Éditer sa playlist Tidal depuis l'app mobile Tidal et retrouver les changements dans Tune.

```mermaid
sequenceDiagram
    participant TI as App Tidal (mobile)
    participant U as Utilisateur
    participant T as Tune
    participant DB as Base locale

    TI->>TI: Ajouter "New Track" à la playlist
    Note over U: Plus tard...
    U->>T: Onglet Sync → "Sync Now"
    T->>TI: Charger playlist Tidal
    T->>T: Comparer avec local
    T->>DB: Ajouter "New Track"
    T-->>U: +1 track ajouté
```

**Directions disponibles** :
| Direction | Effet |
|-----------|-------|
| **Pull** | Tidal → Local (ajout des nouveaux morceaux Tidal) |
| **Push** | Local → Tidal (envoie les ajouts locaux vers Tidal) |
| **Bidirectionnel** | Les deux sens avec détection de conflits |

---

## 5. Export pour partage

**Scénario** : Partager une playlist avec un ami qui n'a pas Tune.

```mermaid
graph LR
    PL[Playlist Tune] -->|Export| F{Format ?}
    F -->|CSV| CSV[playlist.csv<br>Excel / Sheets]
    F -->|JSON| JSON[playlist.json<br>Structuré]
    F -->|Text| TXT[playlist.txt<br>Lisible humain]
    F -->|XSPF| XSPF[playlist.xspf<br>Standard XML]
    
    style PL fill:#7574F3,color:#fff
```

**Formats** :
- **CSV** : Ouvrable dans Excel, Google Sheets, Soundiiz
- **JSON** : Pour les développeurs ou les outils automatisés
- **Text** : `1. Artist - Title [Album]` — lisible par tous
- **XSPF** : Standard XML, compatible VLC, Clementine, etc.

---

## 6. Import d'une playlist externe

**Scénario** : Un ami envoie sa playlist en CSV.

```mermaid
sequenceDiagram
    participant U as Utilisateur
    participant T as Tune
    participant LIB as Bibliothèque

    U->>T: Upload playlist.csv
    T->>T: Parser CSV (50 tracks)
    loop Pour chaque track
        T->>LIB: Recherche titre + artiste
        LIB-->>T: Match trouvé ?
    end
    T->>T: Créer playlist locale
    T-->>U: 40/50 matchés à la bibliothèque
```

---

## 7. Backup complet avant formatage

**Scénario** : Réinstallation du serveur — sauvegarder toutes les playlists.

```mermaid
graph TD
    subgraph Sources
        L[Local<br>12 playlists]
        TI[Tidal<br>280 playlists]
        D[Deezer<br>7 playlists]
    end
    
    Sources -->|Backup| SNAP[Snapshot JSON<br>Noms + tracks + métadonnées]
    SNAP -->|Après réinstall| RESTORE[Restauration]
    
    style SNAP fill:#7574F3,color:#fff
    style RESTORE fill:#10B981,color:#fff
```

**Ce qui est sauvegardé** : nom de la playlist, service d'origine, et pour chaque track : titre, artiste, album, durée, ISRC.

---

## 8. Récupération de morceaux indisponibles

**Scénario** : Des morceaux de ta playlist Tidal ont été retirés du catalogue. Tune cherche des alternatives.

```mermaid
graph LR
    PL[Playlist avec<br>3 tracks indisponibles] -->|Recover| PM[Playlist Manager]
    PM -->|Recherche alternatives| Q[Qobuz]
    PM -->|Recherche alternatives| D[Deezer]
    PM -->|Recherche alternatives| LIB[Bibliothèque locale]
    PM -->|Suggestions| U[Utilisateur valide]
    
    style PM fill:#7574F3,color:#fff
```

---

## Accès rapide

### Web client (.18)

| Fonctionnalité | Où ? |
|---------------|------|
| Voir playlists par source | Sidebar → **Playlists** (icônes en haut) |
| Transférer / Diff / Recover | Sidebar → **Gestionnaire de playlists** |
| Batch transfer | Gestionnaire → onglet **Backup** |
| Historique des transferts | Gestionnaire → onglet **Transferts** |
| Liens de sync | Gestionnaire → onglet **Sync** |

### iOS / iPadOS

| Fonctionnalité | Où ? |
|---------------|------|
| Voir playlists par source | Plus → **Playlists** |
| Gestionnaire | Plus → **Gestionnaire** (ou sidebar iPad) |
| Menu contextuel | Long-press sur une playlist |
| Transferts / Sync / Backup | Onglets dans le Gestionnaire |

---

## Tableau récapitulatif

| Cas d'usage | Complexité | Disponible |
|-------------|-----------|------------|
| Transfert single | Simple | ✅ Web + iOS |
| Batch transfer (tout) | Moyen | ✅ Web + iOS |
| Fusion/merge | Moyen | ✅ API |
| Sync Pull/Push | Moyen | ✅ Web + iOS |
| Export CSV/JSON/XSPF | Simple | ✅ API |
| Import fichier | Simple | ✅ API |
| Backup complet | Simple | ✅ Web + iOS |
| Recover indisponibles | Avancé | ✅ Web |
| Context menu iOS | Simple | ✅ Build 4 |
