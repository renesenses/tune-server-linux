# Configuration Qobuz

Tune Server se connecte à Qobuz via son API web. L'authentification nécessite un App ID et un App Secret (identifiants développeur), ainsi que les identifiants du compte utilisateur (email/mot de passe).

## Prérequis

- Un compte Qobuz actif (Studio, Sublime, ou Studio Premier)
- Des identifiants API Qobuz (App ID et App Secret)

> **Note :** L'API Qobuz n'est pas publiquement accessible aux développeurs tiers. Les identifiants App ID / App Secret sont nécessaires pour accéder à l'API de streaming.

## 1. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_QOBUZ_ENABLED=true
TUNE_QOBUZ_APP_ID=123456789
TUNE_QOBUZ_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxx
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_QOBUZ_ENABLED` | Active le connecteur Qobuz | `false` |
| `TUNE_QOBUZ_APP_ID` | App ID de l'API Qobuz | _(requis)_ |
| `TUNE_QOBUZ_APP_SECRET` | App Secret de l'API Qobuz | _(requis)_ |

### Qualité audio

Qobuz streaming en **FLAC** exclusivement, avec la qualité maximale disponible selon votre abonnement :

| Abonnement | Qualité maximale |
|------------|-----------------|
| Studio | FLAC 44.1 kHz / 16-bit (qualité CD) |
| Studio Premier / Sublime | FLAC jusqu'à 192 kHz / 24-bit (Hi-Res) |

> **Note :** La résolution (sample rate / bit depth) est automatiquement celle de la meilleure version disponible dans le catalogue Qobuz pour chaque morceau.

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 2. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. Qobuz apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à Qobuz**
5. Entrer votre **email** et **mot de passe** Qobuz
6. Le statut passe à "Connecté"

Les tokens sont sauvegardés en base de données et restaurés automatiquement au redémarrage du serveur.

## Fonctionnalités

| Fonctionnalité | Disponible |
|----------------|------------|
| Recherche (tracks, albums, artistes) | Oui |
| Navigation albums et artistes | Oui |
| Playlists personnelles | Oui |
| Contenu recommandé (Home) | Oui (Nouvelles sorties, Meilleures ventes, Récompenses presse, Sélection de la rédaction) |
| Streaming audio complet | Oui (FLAC Hi-Res selon abonnement) |

## Dépannage

### Le bouton Qobuz n'apparaît pas dans les paramètres
- Vérifier que `TUNE_QOBUZ_ENABLED=true` est bien dans le `.env`
- Vérifier que `TUNE_QOBUZ_APP_ID` et `TUNE_QOBUZ_APP_SECRET` sont configurés
- Vérifier que le serveur a été redémarré après la modification

### Erreur "401 Unauthorized" lors de la connexion
- Vérifier que l'email et le mot de passe sont corrects
- Vérifier que le compte Qobuz est actif
- Vérifier que les identifiants App ID / App Secret sont valides

### Le token expire
- Le service détecte automatiquement les tokens expirés (réponse 401) et les supprime
- Il faut se reconnecter depuis les paramètres si le token a expiré

### Pas de Hi-Res
- La résolution dépend de votre abonnement Qobuz
- Vérifier votre abonnement (Studio Premier ou Sublime requis pour le Hi-Res)
- La résolution dépend aussi du catalogue : tous les albums ne sont pas disponibles en Hi-Res
