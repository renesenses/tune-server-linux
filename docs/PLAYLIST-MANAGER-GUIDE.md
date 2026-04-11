# Tune — Gestionnaire de Playlists

## Vue d'ensemble

Le Gestionnaire de Playlists permet de transférer, synchroniser, sauvegarder et fusionner vos playlists entre tous vos services de streaming et votre bibliothèque locale.

```mermaid
graph LR
    subgraph Services
        T[Tidal]
        Q[Qobuz]
        D[Deezer]
        Y[YouTube]
        S[Spotify]
    end
    
    subgraph Tune
        PM[Playlist Manager]
        DB[(Base locale)]
    end
    
    T <-->|Transfer / Sync| PM
    Q <-->|Transfer / Sync| PM
    D -->|Import| PM
    Y -->|Import| PM
    S -->|Import| PM
    PM <--> DB
    
    style PM fill:#7574F3,stroke:#5554D1,color:#fff
    style DB fill:#1A1A1A,stroke:#2A2A2A,color:#E8E8E8
```

---

## 1. Transfert de playlist

Copier une playlist d'un service vers un autre avec matching intelligent des morceaux.

### Comment faire

1. Ouvrir **Gestionnaire de playlists** dans la sidebar
2. Cliquer sur une playlist
3. Cliquer **Transférer**
4. Choisir le service cible
5. Vérifier les résultats du matching
6. Confirmer

### Algorithme de matching

```mermaid
flowchart TD
    A[Morceau source] --> B{ISRC disponible ?}
    B -->|Oui| C[Recherche par ISRC]
    C -->|Trouvé| D[✅ Match exact - score 1.0]
    C -->|Non trouvé| E
    B -->|Non| E[Recherche par artiste + titre]
    E --> F{Titre + artiste identiques ?}
    F -->|Oui| G[✅ Match exact - score 0.95]
    F -->|Non| H[Comparaison fuzzy]
    H --> I{Score > seuil ?}
    I -->|Oui| J[⚠️ Match approximatif]
    I -->|Non| K[Recherche élargie titre seul]
    K --> L{Score > seuil ?}
    L -->|Oui| M[⚠️ Match faible confiance]
    L -->|Non| N[❌ Non trouvé]
    
    style D fill:#10B981,color:#fff
    style G fill:#10B981,color:#fff
    style J fill:#F59E0B,color:#fff
    style M fill:#F59E0B,color:#fff
    style N fill:#EF4444,color:#fff
```

### Scoring composite

| Critère | Poids | Description |
|---------|-------|-------------|
| Titre | 40% | Similarité du titre normalisé |
| Artiste | 30% | Similarité du nom d'artiste |
| Durée | 20% | Proximité de durée (±30s) |
| Album | 10% | Similarité du titre d'album |

### Résultats possibles

- **✅ Matched** (score ≥ 0.9) — correspondance certaine
- **⚠️ Approximate** (score 0.6-0.9) — vérification recommandée
- **❌ Not found** (score < 0.6) — pas de correspondance

---

## 2. Batch Transfer

Transférer **toutes** les playlists d'un service en une seule opération.

### Comment faire

1. Onglet **Backup** dans le gestionnaire
2. Section "Batch Transfer"
3. Sélectionner le service source
4. Sélectionner la destination
5. Cliquer **Transférer tout**

```mermaid
sequenceDiagram
    participant U as Utilisateur
    participant PM as Playlist Manager
    participant S as Service Source
    participant T as Service Cible

    U->>PM: Batch Transfer (Tidal → Local)
    PM->>S: Charger liste playlists
    S-->>PM: 25 playlists
    
    loop Pour chaque playlist
        PM->>S: Charger tracks
        S-->>PM: N tracks
        PM->>T: Matching tracks
        T-->>PM: Résultats matching
        PM->>PM: Créer playlist locale
        PM-->>U: Progression (WebSocket)
    end
    
    PM-->>U: Résultat final
```

---

## 3. Synchronisation

Maintenir une playlist synchronisée entre le local et un service.

### Directions de sync

```mermaid
graph LR
    L[Playlist Locale] -->|Pull| R[Playlist Remote]
    L -->|Push| R
    L <-->|Bidirectionnel| R
    
    style L fill:#7574F3,color:#fff
    style R fill:#3B82F6,color:#fff
```

| Direction | Description |
|-----------|-------------|
| **Pull** | Remote → Local : les nouveaux morceaux du service sont ajoutés localement |
| **Push** | Local → Remote : les morceaux locaux sont ajoutés sur le service |
| **Bidirectionnel** | Les deux sens, avec détection de conflits |

### Comment créer un lien sync

1. Onglet **Sync**
2. Cliquer **Créer un lien**
3. Sélectionner la playlist locale
4. Sélectionner le service + playlist distante
5. Choisir la direction
6. Cliquer **Sync Now** pour synchroniser

---

## 4. Backup

Sauvegarder un snapshot de toutes vos playlists (métadonnées + tracks).

### Comment faire

1. Onglet **Backup**
2. Cliquer **Backup toutes les playlists**
3. Le système sauvegarde chaque playlist avec ses tracks en JSON

### Ce qui est sauvegardé

- Nom de la playlist
- Service d'origine
- Pour chaque track : titre, artiste, album, durée, ISRC

---

## 5. Merge (Fusion)

Combiner plusieurs playlists en une seule.

```mermaid
graph TD
    P1[Playlist Tidal<br>50 tracks] --> M{Merge +<br>Déduplicate}
    P2[Playlist Qobuz<br>45 tracks] --> M
    P3[Playlist Locale<br>30 tracks] --> M
    M --> R[Playlist fusionnée<br>95 tracks uniques]
    
    style M fill:#7574F3,color:#fff
    style R fill:#10B981,color:#fff
```

- **Déduplification** : les doublons (même titre + artiste) sont supprimés
- Les tracks sont matchées à la bibliothèque locale quand possible

---

## 6. Export / Import

### Formats supportés

| Format | Extension | Description |
|--------|-----------|-------------|
| CSV | .csv | Tableur standard (Excel, Google Sheets) |
| JSON | .json | Structuré avec métadonnées complètes |
| XSPF | .xspf | XML Shareable Playlist Format (standard) |
| Text | .txt | Lisible humain : `Artist - Title [Album]` |

### Export

1. Ouvrir une playlist dans le gestionnaire
2. Actions → **Exporter**
3. Choisir le format
4. Le fichier est téléchargé

### Import

1. Gestionnaire → **Importer**
2. Sélectionner le fichier
3. Le système crée une playlist locale
4. Les tracks sont automatiquement matchées à la bibliothèque

---

## 7. Historique

Chaque opération (transfert, sync, backup) est enregistrée dans l'historique.

1. Onglet **Transferts**
2. Liste chronologique des opérations
3. Pour chaque entrée : service source/cible, nombre de tracks matchées/approximatives/non trouvées

---

## API Endpoints

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| GET | `/playlist-manager/services` | Capabilities des services |
| POST | `/playlist-manager/transfer` | Transfert avec matching |
| POST | `/playlist-manager/batch-transfer` | Transfert en lot |
| POST | `/playlist-manager/merge` | Fusion de playlists |
| POST | `/playlist-manager/backup` | Backup toutes playlists |
| POST | `/playlist-manager/export` | Export fichier |
| POST | `/playlist-manager/import` | Import fichier |
| GET | `/playlist-manager/links` | Liens sync |
| POST | `/playlist-manager/links` | Créer lien sync |
| POST | `/playlist-manager/links/{id}/sync` | Sync maintenant |
| DELETE | `/playlist-manager/links/{id}` | Supprimer lien |
| GET | `/playlist-manager/history` | Historique |
| GET | `/playlist-manager/history/{id}` | Détail transfert |
