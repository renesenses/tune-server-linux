# Cahier de Tests — Gestionnaire de Playlists

## T1. Transfert simple

### T1.1 Transfert Tidal → Local
- [ ] Sélectionner une playlist Tidal avec 10+ morceaux
- [ ] Cliquer Transférer → cible "Local"
- [ ] Vérifier que le matching démarre (indicateur de progression)
- [ ] Résultat : playlist locale créée avec les tracks matchées
- [ ] Vérifier les stats : matched / approximate / not_found

### T1.2 Transfert Qobuz → Local
- [ ] Même test avec une playlist Qobuz
- [ ] Vérifier que les morceaux Hi-Res sont correctement matchés

### T1.3 Transfert Local → Tidal (write-back)
- [ ] Sélectionner une playlist locale
- [ ] Transférer vers Tidal avec "Créer sur le service" activé
- [ ] Vérifier que la playlist apparaît dans Tidal
- [ ] Vérifier le nombre de tracks ajoutées

### T1.4 Dry Run (aperçu)
- [ ] Lancer un transfert avec dry_run=true
- [ ] Vérifier qu'aucune playlist n'est créée
- [ ] Vérifier que les résultats de matching sont affichés

### T1.5 Seuil de matching configurable
- [ ] Transférer avec seuil 0.9 → moins de matches approximatifs
- [ ] Transférer avec seuil 0.5 → plus de matches approximatifs
- [ ] Vérifier la cohérence des résultats

---

## T2. Matching

### T2.1 Match ISRC
- [ ] Transférer un morceau avec ISRC connu
- [ ] Vérifier que le match_method = "isrc" et score = 1.0

### T2.2 Match exact (titre + artiste)
- [ ] Transférer "Stairway to Heaven" de "Led Zeppelin"
- [ ] Vérifier match exact (score ≥ 0.95)

### T2.3 Match fuzzy (variantes)
- [ ] Transférer un morceau "Remastered 2024" → doit matcher l'original
- [ ] Transférer "feat. Artist" → doit matcher sans le featuring
- [ ] Transférer "Live" → doit matcher la version studio (score plus bas)

### T2.4 Non trouvé
- [ ] Transférer un morceau très obscur/rare
- [ ] Vérifier status = "not_found"
- [ ] Vérifier que des alternatives sont proposées

### T2.5 Normalisation unicode
- [ ] Morceau avec accents (é, ü, ñ) → doit matcher sans accents
- [ ] Artiste "The Beatles" → doit matcher "Beatles"

---

## T3. Batch Transfer

### T3.1 Toutes les playlists
- [ ] Batch transfer Tidal → Local (toutes les playlists)
- [ ] Vérifier que chaque playlist est transférée
- [ ] Vérifier la progression via WebSocket
- [ ] Vérifier le résultat final (nombre de playlists traitées)

### T3.2 Sélection de playlists
- [ ] Batch transfer avec playlist_ids spécifiques
- [ ] Vérifier que seules les playlists sélectionnées sont traitées

---

## T4. Synchronisation

### T4.1 Créer un lien sync
- [ ] Créer un lien entre playlist locale et playlist Tidal
- [ ] Vérifier que le lien apparaît dans l'onglet Sync
- [ ] Vérifier la direction (pull/push/bidirectional)

### T4.2 Sync Pull
- [ ] Ajouter un morceau sur la playlist Tidal
- [ ] Déclencher le sync
- [ ] Vérifier que le morceau est ajouté à la playlist locale

### T4.3 Sync Push
- [ ] Ajouter un morceau à la playlist locale
- [ ] Déclencher le sync (direction push)
- [ ] Vérifier que le morceau est ajouté sur Tidal

### T4.4 Supprimer un lien
- [ ] Supprimer un lien sync
- [ ] Vérifier qu'il disparaît de la liste
- [ ] Vérifier que les playlists ne sont pas supprimées

---

## T5. Backup

### T5.1 Backup complet
- [ ] Cliquer "Backup toutes les playlists"
- [ ] Vérifier le nombre de playlists sauvegardées
- [ ] Vérifier le nombre total de tracks snapshottés

### T5.2 Backup par service
- [ ] Backup uniquement Tidal
- [ ] Vérifier que seules les playlists Tidal sont sauvegardées

---

## T6. Merge (Fusion)

### T6.1 Fusion simple
- [ ] Sélectionner 2 playlists locales
- [ ] Fusionner avec déduplification
- [ ] Vérifier que les doublons sont supprimés
- [ ] Vérifier le nombre de tracks dans la playlist résultante

### T6.2 Fusion cross-service
- [ ] Fusionner 1 playlist Tidal + 1 playlist locale
- [ ] Vérifier que les tracks des deux sources sont présentes
- [ ] Vérifier la déduplification cross-service

---

## T7. Export / Import

### T7.1 Export CSV
- [ ] Exporter une playlist en CSV
- [ ] Ouvrir dans Excel/Google Sheets
- [ ] Vérifier les colonnes : Title, Artist, Album, Duration, ISRC

### T7.2 Export JSON
- [ ] Exporter en JSON
- [ ] Vérifier la structure (playlist name, track_count, tracks array)

### T7.3 Export XSPF
- [ ] Exporter en XSPF
- [ ] Valider le XML (bien formé)
- [ ] Ouvrir dans VLC → vérifier la lecture

### T7.4 Export Text
- [ ] Exporter en texte
- [ ] Vérifier le format "Artist - Title [Album]"

### T7.5 Import CSV
- [ ] Importer le CSV exporté en T7.1
- [ ] Vérifier qu'une playlist locale est créée
- [ ] Vérifier le nombre de tracks matchées à la bibliothèque

### T7.6 Import JSON
- [ ] Importer le JSON exporté en T7.2
- [ ] Vérifier la création de la playlist

### T7.7 Import round-trip
- [ ] Exporter → Importer → Vérifier que le contenu est identique

---

## T8. Historique

### T8.1 Enregistrement
- [ ] Effectuer un transfert
- [ ] Vérifier qu'il apparaît dans l'onglet Transferts
- [ ] Vérifier les infos : source, cible, stats, date

### T8.2 Détail
- [ ] Cliquer sur une entrée d'historique
- [ ] Vérifier le détail par track (status, score, méthode)
- [ ] Vérifier les alternatives proposées pour les non-trouvés

---

## T9. Services capabilities

### T9.1 Vérifier les capabilities
- [ ] GET /playlist-manager/services
- [ ] Tidal : supports_write = true
- [ ] Qobuz : supports_write = true
- [ ] YouTube : supports_write = false
- [ ] Local : supports_write = true

---

## T10. Edge cases

### T10.1 Playlist vide
- [ ] Transférer une playlist avec 0 tracks
- [ ] Vérifier que rien ne plante

### T10.2 Service non authentifié
- [ ] Tenter un transfert depuis un service non connecté
- [ ] Vérifier le message d'erreur

### T10.3 Très grande playlist (500+ tracks)
- [ ] Transférer une playlist de 500+ morceaux
- [ ] Vérifier la progression (pas de timeout)
- [ ] Vérifier le rate limiting (pas de ban API)

### T10.4 Caractères spéciaux
- [ ] Playlist avec émojis dans le nom
- [ ] Tracks avec caractères japonais/chinois/coréens
- [ ] Vérifier l'export/import avec ces caractères

### T10.5 Connexion perdue pendant un transfert
- [ ] Couper le réseau pendant un batch transfer
- [ ] Vérifier que le status = "failed" dans l'historique
- [ ] Vérifier que les playlists partiellement transférées sont conservées
