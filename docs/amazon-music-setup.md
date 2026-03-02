# Configuration Amazon Music

Tune Server se connecte à Amazon Music via son API interne. L'authentification utilise le flux OAuth Device Code — aucune inscription développeur n'est nécessaire.

> **Note :** Ce connecteur utilise l'API interne (non documentée) d'Amazon Music. Il peut cesser de fonctionner si Amazon modifie son API.

## Prérequis

- Un compte Amazon avec un abonnement Amazon Music actif
- La qualité de streaming dépend de votre abonnement :

| Abonnement | Qualités accessibles |
|------------|---------------------|
| Amazon Music Free | SD (AAC) |
| Amazon Music Prime | SD (AAC) |
| Amazon Music Unlimited | SD, HD (FLAC 44.1kHz/16-bit), ULTRA_HD (FLAC 96kHz/24-bit) |

## 1. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_AMAZON_MUSIC_ENABLED=true
TUNE_AMAZON_MUSIC_REGION=fr
TUNE_AMAZON_MUSIC_QUALITY=HD
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_AMAZON_MUSIC_ENABLED` | Active le connecteur Amazon Music | `false` |
| `TUNE_AMAZON_MUSIC_REGION` | Région géographique | `us` |
| `TUNE_AMAZON_MUSIC_QUALITY` | Qualité de streaming | `HD` |

### Niveaux de qualité

| Valeur | Format | Qualité |
|--------|--------|---------|
| `SD` | AAC | 44.1 kHz / 16-bit |
| `HD` | FLAC | 44.1 kHz / 16-bit (qualité CD) |
| `ULTRA_HD` | FLAC | 96 kHz / 24-bit (Hi-Res) |

### Régions supportées

| Région | Domaine |
|--------|---------|
| `us` | amazon.com |
| `uk` | amazon.co.uk |
| `de` | amazon.de |
| `fr` | amazon.fr |
| `it` | amazon.it |
| `es` | amazon.es |
| `jp` | amazon.co.jp |
| `ca` | amazon.ca |
| `au` | amazon.com.au |
| `br` | amazon.com.br |
| `mx` | amazon.com.mx |
| `in` | amazon.in |

> **Important :** La région doit correspondre à celle de votre compte Amazon. Un compte Amazon.fr doit utiliser la région `fr`.

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 2. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. Amazon Music apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à Amazon Music**
5. Un code et un lien d'autorisation s'affichent
6. Ouvrir le lien dans un navigateur et se connecter avec votre compte Amazon
7. Entrer le code affiché si demandé
8. Autoriser l'application
9. Le statut passe automatiquement à "Connecté" (polling en arrière-plan)

Les tokens sont sauvegardés en base de données et restaurés automatiquement au redémarrage du serveur. Le rafraîchissement des tokens est automatique.

## Fonctionnalités

| Fonctionnalité | Disponible |
|----------------|------------|
| Recherche (tracks, albums, artistes) | Oui |
| Navigation albums et artistes | Oui |
| Playlists personnelles | Non |
| Streaming audio complet | Oui (FLAC Hi-Res selon abonnement) |

## Dépannage

### Le bouton Amazon Music n'apparaît pas dans les paramètres
- Vérifier que `TUNE_AMAZON_MUSIC_ENABLED=true` est bien dans le `.env`
- Vérifier que le serveur a été redémarré après la modification

### Erreur lors de l'autorisation
- Le code d'autorisation expire après 5 minutes — réessayer si nécessaire
- Vérifier que votre compte Amazon a un abonnement Music actif
- Vérifier que la région configurée correspond à votre compte Amazon

### Pas de streaming ou qualité réduite
- Vérifier votre abonnement Amazon Music (Unlimited requis pour HD/ULTRA_HD)
- Vérifier que la région est correcte

### Erreur "API changed" ou comportement inattendu
- Ce connecteur utilise l'API interne d'Amazon Music
- Mettre à jour Tune Server vers la dernière version
- Vérifier les logs pour les détails de l'erreur

### Le token expire
- Le rafraîchissement est automatique
- Si le rafraîchissement échoue, il faut se reconnecter depuis les paramètres
