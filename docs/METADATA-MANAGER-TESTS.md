# Cahier de Tests — Gestionnaire de Métadonnées (v0.5.8)

## T1. Édition manuelle

### T1.1 Éditer un track (DB)
- [ ] PATCH /metadata/tracks/{id} avec titre modifié
- [ ] Vérifier que le titre est mis à jour en DB
- [ ] Vérifier que le fichier audio n'est PAS modifié
- [ ] Tester les champs : genre, composer, year, lyrics, comment, ISRC, BPM

### T1.2 Éditer un album (DB)
- [ ] PATCH /metadata/albums/{id} avec année + label
- [ ] Vérifier la mise à jour en DB
- [ ] Tester : musicbrainz_release_id, catalog_number, barcode

### T1.3 Éditer un artiste (DB)
- [ ] PATCH /metadata/artists/{id} avec bio + sort_name
- [ ] Vérifier la mise à jour

### T1.4 Custom tags (JSON)
- [ ] PATCH track avec custom_tags: {"mood": "chill", "rating": 5}
- [ ] Vérifier le stockage JSON en DB
- [ ] Relire et vérifier la structure

---

## T2. Écriture tags fichier

### T2.1 Lire tags depuis fichier
- [ ] GET /metadata/tracks/{id}/tags sur un FLAC
- [ ] GET /metadata/tracks/{id}/tags sur un MP3
- [ ] Vérifier que les tags lus correspondent au fichier

### T2.2 Écrire tags dans un FLAC
- [ ] Modifier genre + year en DB
- [ ] POST /metadata/tracks/{id}/write-tags
- [ ] Relire le fichier FLAC avec un outil externe (MediaInfo/ffprobe)
- [ ] Vérifier que les tags sont écrits correctement

### T2.3 Écrire tags dans un MP3
- [ ] Même test avec un fichier MP3 (tags ID3v2)
- [ ] Vérifier TIT2, TPE1, TALB, TCON, TDRC

### T2.4 Écrire tags album entier
- [ ] POST /metadata/albums/{id}/write-tags
- [ ] Vérifier que TOUS les tracks de l'album sont mis à jour
- [ ] Vérifier le compteur success vs errors

### T2.5 Fichier manquant
- [ ] Tenter write-tags sur un track sans file_path
- [ ] Vérifier le message d'erreur 400

---

## T3. Batch edit

### T3.1 Modifier plusieurs tracks
- [ ] POST /metadata/batch/tracks avec 10 track_ids + genre = "Jazz"
- [ ] Vérifier que les 10 tracks ont le genre "Jazz"

### T3.2 Rename artiste global
- [ ] POST /metadata/batch/rename-artist old="Beatles" new="The Beatles"
- [ ] Vérifier la table artists (nom modifié)
- [ ] Vérifier la table tracks (artist_name modifié sur tous les morceaux)
- [ ] Vérifier la table albums (artist_name modifié)

### T3.3 Rename avec écriture fichiers
- [ ] POST /metadata/batch/rename-artist avec update_files=true
- [ ] Vérifier que les tags des fichiers sont aussi modifiés

### T3.4 Batch write-tags
- [ ] POST /metadata/batch/write-tags avec 5 track_ids
- [ ] Vérifier le résultat (success count)
- [ ] Vérifier les tags dans les fichiers

---

## T4. MusicBrainz lookup

### T4.1 Recherche track
- [ ] POST /metadata/lookup title="Stairway to Heaven" artist="Led Zeppelin"
- [ ] Vérifier les résultats (titre, artiste, ISRC, MB recording ID, year)
- [ ] Vérifier le score de pertinence

### T4.2 Recherche album
- [ ] POST /metadata/lookup-album title="Kind of Blue" artist="Miles Davis"
- [ ] Vérifier : MB release ID, year, label, barcode

### T4.3 Track introuvable
- [ ] POST /metadata/lookup title="xyzabc123" artist="unknown"
- [ ] Vérifier que results est vide

### T4.4 Rate limiting
- [ ] Lancer 5 lookups consécutifs
- [ ] Vérifier qu'aucun n'échoue par rate limit (1 req/s)

---

## T5. Enrichissement multi-source

### T5.1 Enrichir un track
- [ ] POST /metadata/enrich/{id} sur un track sans ISRC ni genre
- [ ] Vérifier que des suggestions sont créées en DB
- [ ] Vérifier les sources (musicbrainz, lastfm)

### T5.2 Auto-apply haute confiance
- [ ] Enrichir un track bien connu (ex: "Bohemian Rhapsody" de "Queen")
- [ ] Vérifier que l'ISRC et le MB ID sont auto-appliqués (confiance > 0.9)

### T5.3 Enrichir un album
- [ ] POST /metadata/enrich-album/{id}
- [ ] Vérifier les suggestions (MB release ID, year, label)
- [ ] Vérifier si une cover a été téléchargée

### T5.4 Last.fm tags
- [ ] Vérifier que les genres/tags Last.fm apparaissent dans les suggestions
- [ ] Tester avec et sans LASTFM_API_KEY configurée

---

## T6. Covers

### T6.1 Recherche covers
- [ ] GET /metadata/covers/search?album="Kind of Blue"&artist="Miles Davis"
- [ ] Vérifier que des résultats sont retournés (Cover Art Archive et/ou Discogs)

### T6.2 Fetch cover pour un album
- [ ] POST /metadata/covers/album/{id} sur un album sans cover
- [ ] Vérifier que cover_path est mis à jour en DB
- [ ] Vérifier que le fichier image existe dans artwork_cache

### T6.3 Album avec MB release ID
- [ ] Enrichir un album d'abord (pour obtenir le MB release ID)
- [ ] Fetch cover via Cover Art Archive
- [ ] Vérifier la qualité de l'image (>10KB)

### T6.4 Embed cover dans fichier
- [ ] POST /metadata/covers/track/{id}/embed sur un FLAC
- [ ] Vérifier avec ffprobe que l'image est intégrée
- [ ] Tester sur un MP3 (ID3 APIC)

### T6.5 Album sans cover disponible
- [ ] Tester sur un album obscur
- [ ] Vérifier le message "No cover found"

---

## T7. Fingerprinting (AcoustID)

### T7.1 Prérequis
- [ ] Vérifier que fpcalc est installé (`which fpcalc`)
- [ ] Si absent : `apt install libchromaprint-tools` ou `brew install chromaprint`

### T7.2 Identifier un track
- [ ] POST /metadata/fingerprint/{id} sur un morceau connu
- [ ] Vérifier : identified=true, titre, artiste, MB recording ID
- [ ] Vérifier le score (> 0.5)

### T7.3 Track non identifiable
- [ ] POST /metadata/fingerprint/{id} sur un enregistrement privé/rare
- [ ] Vérifier : identified=false

### T7.4 Batch fingerprint
- [ ] POST /metadata/fingerprint-batch sans IDs (scanne les non-identifiés)
- [ ] Vérifier le rapport (identified, not_found, errors)
- [ ] Vérifier que les suggestions sont créées en DB

### T7.5 Batch avec IDs spécifiques
- [ ] POST /metadata/fingerprint-batch avec 5 track_ids
- [ ] Vérifier que seuls ces 5 sont traités

---

## T8. Auto-fix background

### T8.1 Lancer un scan
- [ ] POST /metadata/auto-fix
- [ ] Vérifier status = "started"
- [ ] GET /metadata/auto-fix/status → status = "running"

### T8.2 Progression
- [ ] Attendre quelques secondes
- [ ] GET /metadata/auto-fix/status
- [ ] Vérifier current/total/fixed/suggestions qui progressent

### T8.3 Résultat
- [ ] Attendre la fin du scan
- [ ] GET /metadata/auto-fix/status → status = "completed"
- [ ] Vérifier fixed > 0 (si des tracks incomplets existent)

### T8.4 Rapport
- [ ] GET /metadata/auto-fix/report
- [ ] Vérifier les champs : started_at, completed_at, tracks_scanned, auto_fixed, suggestions

### T8.5 Scan déjà en cours
- [ ] Lancer un scan pendant qu'un autre tourne
- [ ] Vérifier le message "Scan already in progress"

### T8.6 WebSocket events
- [ ] Connecter un WebSocket
- [ ] Vérifier les events metadata.autofix.started/progress/completed

---

## T9. Suggestions

### T9.1 Lister les suggestions
- [ ] GET /metadata/suggestions
- [ ] Vérifier les champs : field, current_value, suggested_value, source, confidence

### T9.2 Accepter une suggestion
- [ ] POST /metadata/suggestions/{id}/accept
- [ ] Vérifier que la valeur est appliquée au track/album
- [ ] Vérifier que le status passe à "accepted"

### T9.3 Rejeter une suggestion
- [ ] POST /metadata/suggestions/{id}/reject
- [ ] Vérifier que le status passe à "rejected"
- [ ] Vérifier que le track n'est PAS modifié

### T9.4 Accepter tout (seuil)
- [ ] POST /metadata/suggestions/accept-all?min_confidence=0.9
- [ ] Vérifier que seules les suggestions ≥ 0.9 sont appliquées
- [ ] Vérifier le nombre d'applied

---

## T10. Doublons

### T10.1 Scanner les doublons
- [ ] POST /metadata/duplicates/scan
- [ ] Vérifier total_scanned et duplicates_found
- [ ] Vérifier que les groupes contiennent bien les mêmes morceaux

### T10.2 Hash cohérent
- [ ] Scanner deux fois
- [ ] Vérifier que les hash audio_hash en DB sont identiques (déterministe)

### T10.3 Lister les doublons
- [ ] GET /metadata/duplicates
- [ ] Vérifier track_a et track_b avec titres et paths

### T10.4 Résoudre un doublon
- [ ] POST /metadata/duplicates/resolve avec keep_track_id
- [ ] Vérifier resolved = 1
- [ ] Vérifier que le track "removable" n'est PAS supprimé automatiquement

### T10.5 Pas de faux positifs
- [ ] Vérifier qu'un même morceau en FLAC et MP3 n'est PAS signalé comme doublon
- [ ] Seuls les vrais doublons binaires (même contenu audio) sont détectés

---

## T11. Edge cases

### T11.1 Track streaming (pas de fichier)
- [ ] Tenter write-tags sur un track Tidal (source != local)
- [ ] Vérifier message d'erreur approprié

### T11.2 Fichier corrompu
- [ ] Tenter fingerprint sur un fichier corrompu
- [ ] Vérifier que l'erreur est gérée proprement

### T11.3 Caractères spéciaux
- [ ] Éditer un titre avec accents, émojis, caractères CJK
- [ ] Vérifier l'écriture dans les tags (encodage UTF-8)

### T11.4 Album sans tracks
- [ ] Tenter write-tags sur un album vide
- [ ] Vérifier tracks_processed = 0

### T11.5 MusicBrainz indisponible
- [ ] Simuler un timeout MusicBrainz
- [ ] Vérifier que l'enrichissement retourne gracieusement (pas de crash)

### T11.6 Permissions fichier
- [ ] Tenter write-tags sur un fichier en lecture seule
- [ ] Vérifier le message d'erreur
