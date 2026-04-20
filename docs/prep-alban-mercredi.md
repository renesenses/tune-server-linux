# Préparation démo Alban Amouroux — Mercredi 22 avril 15h-16h

## Questions & Réponses

### Business & positionnement

**1. "Gratuit et open source — comment vous gagnez de l'argent ?"**
Aujourd'hui c'est un projet passion porté par une conviction : l'audio de qualité ne devrait pas coûter 830$. Le modèle économique viendra avec la communauté — partenariats hardware (OEM), services premium, consulting/intégration. Le code reste open source. La valeur est dans l'écosystème, le support, et l'intégration — pas dans le verrouillage du logiciel.

**2. "Roon a 10 ans d'avance. Pourquoi un audiophile choisirait Tune ?"**
Le prix (gratuit vs 830$), l'ouverture (3 services opérationnels vs 2, standards ouverts vs RAAT propriétaire), et la vérifiabilité (MD5 vs signal path indicatif). On ne prétend pas remplacer Roon pour tout le monde — on propose une alternative libre, gratuite et vérifiable pour ceux qui veulent garder le contrôle de leur installation.

**3. "Vous êtes seul développeur. Que se passe-t-il si vous arrêtez ?"**
Le code est public sur GitHub. Claude AI me permet de développer à la vitesse d'une équipe de 5 personnes — 3 versions en 2 jours ce weekend. Une communauté de bêta testeurs remonte bugs et idées. 750+ tests, code propre et documenté. Le vrai risque pour un audiophile, c'est de dépendre d'une entreprise fermée qui peut augmenter ses prix ou fermer.

**4. "Combien d'utilisateurs actifs ?"**
15 bêta testeurs actifs qui remontent du feedback chaque semaine. On est en phase qualité, pas en phase volume. On compte sur la presse et la communauté pour favoriser l'adoption. Les retours des utilisateurs sont garants de la stabilité.

**5. "Avez-vous des accords avec Tidal, Qobuz, Spotify ?"**
On utilise les API publiques documentées, comme le font des dizaines d'applications. Un contrat commercial viendra avec la taille de la base utilisateurs.

---

### Qualité audio

**6. "Le checksum MD5 ne dit rien sur la qualité d'écoute réelle, non ?"**
Le MD5 prouve que Tune ne dégrade pas le signal — pas un seul bit n'a été modifié. Après, la qualité d'écoute dépend du DAC, des enceintes, de la pièce — ça c'est pas notre job. Notre job c'est de garantir que le signal arrive intact au renderer. Et ça, on le prouve mathématiquement.

**7. "Faire passer le signal par un serveur Python ajoute du jitter ?"**
En passthrough, Tune ne touche pas au signal — il relaie les octets bruts vers le renderer via HTTP/TCP. Le jitter est généré par l'horloge du DAC, pas par le serveur. Python ne touche jamais aux échantillons audio. Le serveur n'est qu'un livreur de fichiers.

**8. "DSD via DLNA sur réseau WiFi — un non-sens audiophile ?"**
On aurait dit ça il y a quelques années mais le WiFi se développe, gagne en débit. Un fichier DSD64 c'est 5.6 Mbit/s — le WiFi 5 fait 400 Mbit/s, on a 70 fois la marge nécessaire. Je reste utilisateur filaire — tous mes appareils sont câblés en ethernet — mais ça marche aussi en WiFi sans aucun problème.

**9. "DLNA date de 2003. C'est un handicap face à RAAT ?"**
Non, car le DLNA est un standard du marché partagé avec la quasi-totalité des constructeurs. RAAT c'est techniquement bien, mais ça enferme : un DAC Roon Ready ne fonctionne qu'avec Roon. Un DAC DLNA fonctionne avec tout — Tune, Volumio, BubbleUPnP, n'importe quelle app.

**10. "Bit-perfect vérifié — marketing ou vraie différence ?"**
C'est une preuve technique que le signal est intact. Aucun concurrent ne le fait — ni Roon, ni jPlay, ni Volumio. Ce n'est pas du marketing, c'est de la transparence.

---

### Technique

**11. "Développé avec l'IA — Claude écrit le code à votre place ?"**
Oui, je donne l'intention, je teste, je valide. L'IA me permet de développer à la vitesse d'une équipe de 5 personnes. C'est le meilleur partenaire qu'un développeur puisse avoir — il code, je pilote.

**12. "Un serveur sur iPad peut rivaliser avec un serveur dédié ?"**
La puce M1/M2 de l'iPad est plus puissante que beaucoup de serveurs Linux dédiés à l'audio. Le hardware a considérablement évolué et Tune tire pleinement profit de cette avancée grâce à Hummingbird, un serveur HTTP Swift natif embarqué dans l'app.

**13. "Amazon et Spotify n'ont pas d'API audio officielle. Comment faites-vous ?"**
Honnêtement, 3 services sont pleinement opérationnels aujourd'hui : Tidal, Qobuz et YouTube Music. Amazon est en bêta fermée, Spotify n'a pas d'API audio publique, et Deezer coupe après 30 secondes. Les autres arriveront quand les API s'ouvriront.

**14. "Le multi-room synchronisé en DLNA, c'est fiable ?"**
C'est vrai, DLNA n'a pas été conçu pour le multi-room. C'est Tune qui gère la synchronisation par-dessus — un moteur de sync qui mesure la latence de chaque appareil, compense les délais, et corrige les dérives en temps réel (polling 100ms, précision < 50ms). Ce n'est pas parfait comme Sonos ou RAAT, mais c'est fonctionnel et ça s'améliore à chaque version.

---

### Concurrence

**15. "jPlay revendique le minimalisme réseau. Tune fait le contraire. Contradictoire ?"**
En passthrough, Tune est aussi minimaliste que jPlay — zéro traitement, les octets passent sans être touchés. Mais on a la possibilité d'ajouter du DSP, du multi-room, du stéréo pairing quand l'utilisateur le veut. Garantir la qualité du signal ET apporter des fonctionnalités — ce n'est pas contradictoire, c'est complémentaire.

**16. "Volumio et moOde sont aussi open source et gratuits. Qu'apportez-vous ?"**
Volumio et moOde sont des lecteurs locaux pour Raspberry Pi — sortie audio locale uniquement. Tune est un serveur réseau multi-plateforme qui envoie vers n'importe quel renderer DLNA/AirPlay du réseau. Multi-room synchronisé, apps natives iOS/Android, stéréo pairing, bit-perfect vérifié MD5, 3 services streaming, iPad comme serveur autonome — aucun des deux ne propose ça. Pas la même catégorie.

**17. "Si Roon baisse son prix, quel intérêt pour Tune ?"**
La gratuité. Ma capacité avec l'IA de le faire évoluer à une vitesse qu'aucune entreprise traditionnelle ne peut suivre. Et si Roon devient gratuit un jour... ne serait-ce pas un peu grâce à Tune ?

---

### Vision

**18. "Roadmap après la v1.0 ?"**
On va incorporer de l'IA dans Tune : recommandations personnalisées, biographies artistes enrichies automatiquement, détection de doublons et corrections de métadonnées intelligentes. L'IA qui développe Tune aujourd'hui sera demain dans Tune pour enrichir l'expérience utilisateur.

**19. "Vous visez les audiophiles ou le grand public ?"**
Les deux. On veut rendre audiophiles les consommateurs de son grand public. Aujourd'hui, écouter de la musique en qualité studio est réservé à ceux qui ont le budget pour Roon et le matériel certifié. Tune démocratise ça — gratuit, ouvert, sur n'importe quel appareil.

**20. "Un partenariat hardware est-il envisageable ?"**
C'est même un souhait et un travail à venir. On a déjà des contacts avec des fabricants HiFi en France et en Belgique. L'objectif : proposer une alternative ouverte à BluOS ou HEOS — des écosystèmes fermés qui enferment l'utilisateur.

---

---

## Annexe : RAAT vs DLNA

### RAAT (Roon Advanced Audio Transport)
- Protocole propriétaire Roon Labs (~2015), conçu pour l'audio
- Sync multi-room native < 1ms, gestion du clock par le endpoint
- **Mais** : fermé, certification payante, ~300 appareils, dépendance totale à Roon (830$)

### DLNA/UPnP
- Standard ouvert (Sony, Intel, Microsoft, 2003), basé sur HTTP
- Universel : des milliers d'appareils compatibles, interopérable
- **Mais** : pas de sync multi-room native, pas de gestion HTTPS, quirks fabricants

### Ce que Tune apporte au-dessus de DLNA

| Faiblesse DLNA | Solution Tune |
|---|---|
| Pas de multi-room sync | Moteur de sync (polling 100ms, précision < 50ms, compensation latence par appareil) |
| Pas de HTTPS | Proxy HTTPS→HTTP automatique (Micromega) |
| Pas de redirections CDN | Résolution manuelle des redirections |
| Quirks fabricants | Détection auto (Micromega, DMP-A8, Sonos) + comportement adapté |
| Métadonnées pauvres | DIDL-Lite enrichi + signal path + MusicBrainz/Discogs |
| Pas de vérification signal | Checksum MD5 bout en bout |
| Formats mal annoncés | Heuristique DSD par nom de device |
| Pas de gapless standard | SetNextAVTransportURI + fallback pipeline |

### "Est-ce que Tune utilise RAAT ?"
Non. RAAT est fermé, non documenté, non licenciable. Tune communique via DLNA/UPnP, AirPlay et sortie locale (DAC USB). Mais un appareil "Roon Ready" supporte aussi DLNA dans 95% des cas (Linn, dCS, Hegel, EverSolo) — donc Tune peut leur parler, juste pas via RAAT. La seule perte : la sync < 1ms. Notre moteur fait < 50ms — imperceptible à l'oreille.

### Phrase clé pour Alban
> "RAAT est techniquement supérieur pour le multi-room — c'est indéniable. Mais c'est un protocole fermé qui crée une dépendance à Roon. DLNA est un standard ouvert, universel et pérenne. Ses faiblesses sont exactement ce que Tune corrige par logiciel. On obtient 90% des bénéfices de RAAT avec 100% de la liberté de DLNA."

---

## Checklist démo

- [ ] .18 allumée, v0.6.8, web client à jour
- [ ] DMP-A8 et/ou Micromega allumés
- [ ] Tidal connecté
- [ ] iPhone avec TestFlight à jour
- [ ] Teams testé avec partage d'écran
- [ ] Ce document imprimé ou ouvert à côté

---

*Préparé le 20 avril 2026 — MozAIk Labs*
