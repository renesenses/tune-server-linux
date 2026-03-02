# Configuration Tidal

Tune Server utilise la bibliothèque `tidalapi` pour se connecter à Tidal. L'authentification utilise le flux OAuth Device Code — aucune inscription développeur ni clé API n'est nécessaire.

## Prérequis

- Un compte Tidal actif (Free, HiFi, ou HiFi Plus)
- La qualité de streaming dépend de votre abonnement :

| Abonnement | Qualités accessibles |
|------------|---------------------|
| Free | LOW (96 kbps AAC) |
| HiFi | LOW, HIGH (320 kbps), LOSSLESS (FLAC 44.1kHz/16-bit) |
| HiFi Plus | LOW, HIGH, LOSSLESS, HI_RES_LOSSLESS (FLAC jusqu'à 192kHz/24-bit) |

## 1. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_TIDAL_ENABLED=true
TUNE_TIDAL_QUALITY=LOSSLESS
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_TIDAL_ENABLED` | Active le connecteur Tidal | `false` |
| `TUNE_TIDAL_QUALITY` | Qualité de streaming | `HI_RES_LOSSLESS` |

### Niveaux de qualité

| Valeur | Format | Qualité |
|--------|--------|---------|
| `LOW` | AAC | 96 kbps |
| `HIGH` | AAC | 320 kbps |
| `LOSSLESS` | FLAC | 44.1 kHz / 16-bit (qualité CD) |
| `HI_RES_LOSSLESS` | FLAC | Jusqu'à 192 kHz / 24-bit |

> **Note :** Si la qualité configurée n'est pas accessible avec votre abonnement, le service bascule automatiquement vers le niveau inférieur disponible (HI_RES_LOSSLESS → LOSSLESS → HIGH).

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 2. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. Tidal apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à Tidal**
5. Un lien d'autorisation et un code appareil s'affichent
6. Ouvrir le lien dans un navigateur (peut être sur un autre appareil)
7. Se connecter avec votre compte Tidal et entrer le code
8. Le statut passe automatiquement à "Connecté"

Les tokens sont sauvegardés en base de données et restaurés automatiquement au redémarrage du serveur. Le rafraîchissement des tokens est automatique.

## Fonctionnalités

| Fonctionnalité | Disponible |
|----------------|------------|
| Recherche (tracks, albums, artistes) | Oui |
| Navigation albums et artistes | Oui |
| Playlists personnelles | Oui |
| Contenu recommandé (Home) | Oui (sections éditorialisées) |
| Streaming audio complet | Oui (FLAC HiRes selon abonnement) |

## Dépannage

### Le bouton Tidal n'apparaît pas dans les paramètres
- Vérifier que `TUNE_TIDAL_ENABLED=true` est bien dans le `.env`
- Vérifier que le serveur a été redémarré après la modification

### La qualité est inférieure à celle configurée
- Vérifier votre abonnement Tidal (HiFi Plus requis pour HI_RES_LOSSLESS)
- Le service bascule automatiquement vers la meilleure qualité disponible
- Les logs indiquent le niveau de qualité effectivement utilisé

### L'autorisation échoue ou expire
- Le code d'autorisation expire après 5 minutes — réessayer si nécessaire
- Vérifier que votre compte Tidal est actif

### Le token expire
- Le rafraîchissement est automatique
- Si le rafraîchissement échoue (mot de passe changé, abonnement expiré), il faut se reconnecter depuis les paramètres
