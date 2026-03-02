# Configuration YouTube Music

Tune Server utilise l'API YouTube Music via la bibliothèque `ytmusicapi` pour la recherche, la navigation d'albums/artistes et l'accès aux playlists. Le streaming audio est assuré par `yt-dlp`.

## Prérequis

Depuis ytmusicapi v1.9 (novembre 2024), il est nécessaire de créer ses propres identifiants OAuth Google. Les identifiants intégrés ont été bloqués par Google.

## 1. Créer un projet Google Cloud

1. Se rendre sur **https://console.cloud.google.com/**
2. Créer un nouveau projet (ou utiliser un projet existant)
3. Nommer le projet (ex. `Tune Server`)

## 2. Activer l'API YouTube Data v3

1. Dans le projet, aller dans **APIs & Services** > **Library**
2. Chercher **YouTube Data API v3**
3. Cliquer sur **Enable**

## 3. Configurer l'écran de consentement OAuth

1. Aller dans **APIs & Services** > **OAuth consent screen**
2. Choisir **External** (sauf si vous avez un compte Google Workspace)
3. Remplir les champs obligatoires :
   - **App name** : `Tune Server`
   - **User support email** : votre email
   - **Developer contact** : votre email
4. Ajouter le scope : `https://www.googleapis.com/auth/youtube`
5. Ajouter votre compte Google comme **Test user** (important tant que l'app est en mode Testing)
6. Sauvegarder

## 4. Créer les identifiants OAuth

1. Aller dans **APIs & Services** > **Credentials**
2. Cliquer sur **Create Credentials** > **OAuth client ID**
3. Choisir le type d'application : **TVs and Limited Input devices**
4. Nommer le client (ex. `Tune Server`)
5. Cliquer sur **Create**
6. Copier le **Client ID** et le **Client Secret**

> **Important :** Le type **TVs and Limited Input devices** est requis pour le flux d'autorisation par code appareil (device code flow) utilisé par YouTube Music.

## 5. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_YOUTUBE_ENABLED=true
TUNE_YOUTUBE_CLIENT_ID=123456789-xxxxxxxxx.apps.googleusercontent.com
TUNE_YOUTUBE_CLIENT_SECRET=GOCSPX-xxxxxxxxx
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_YOUTUBE_ENABLED` | Active le connecteur YouTube Music | `false` |
| `TUNE_YOUTUBE_CLIENT_ID` | Client ID OAuth Google | _(requis)_ |
| `TUNE_YOUTUBE_CLIENT_SECRET` | Client Secret OAuth Google | _(requis)_ |
| `TUNE_YOUTUBE_OAUTH_JSON` | _(Legacy)_ Chemin vers un fichier oauth.json existant | `None` |
| `TUNE_YOUTUBE_URL_CACHE_TTL` | Durée de cache des URLs de streaming en secondes | `3600` |

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 6. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. YouTube Music apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à YouTube Music**
5. Un code et un lien d'autorisation s'affichent
6. Cliquer sur le lien, se connecter sur Google et entrer le code si demandé
7. Autoriser l'application
8. Le statut passe automatiquement à "Connecté" (polling en arrière-plan)

## Fonctionnalités

| Fonctionnalité | Disponible |
|----------------|------------|
| Recherche (tracks, albums, artistes) | Oui |
| Navigation albums et artistes | Oui |
| Playlists personnelles | Oui |
| Contenu recommandé (Home) | Oui |
| Streaming audio complet | Oui (via yt-dlp) |

> **Note :** Contrairement à Spotify et Deezer, YouTube Music permet le streaming audio complet via `yt-dlp`. Les URLs sont temporaires (~6h) et sont mises en cache automatiquement.

## Méthode legacy : fichier oauth.json

Si vous avez déjà un fichier `oauth.json` généré par ytmusicapi (versions antérieures), vous pouvez l'utiliser directement :

```env
TUNE_YOUTUBE_OAUTH_JSON=/chemin/vers/oauth.json
```

Dans ce cas, les variables `CLIENT_ID` et `CLIENT_SECRET` ne sont pas nécessaires.

## Dépannage

### Le bouton YouTube Music n'apparaît pas dans les paramètres
- Vérifier que `TUNE_YOUTUBE_ENABLED=true` est bien dans le `.env`
- Vérifier que le serveur a été redémarré après la modification

### Erreur "No credentials" lors de la connexion
- Vérifier que `TUNE_YOUTUBE_CLIENT_ID` et `TUNE_YOUTUBE_CLIENT_SECRET` sont bien configurés
- Vérifier que le type d'application OAuth est bien **TVs and Limited Input devices**

### L'autorisation échoue ou expire
- Vérifier que votre compte Google est ajouté comme **Test user** dans l'écran de consentement
- Le code d'autorisation expire après 30 minutes — réessayer si nécessaire

### Erreur de streaming (yt-dlp)
- Mettre à jour yt-dlp : `pip install -U yt-dlp`
- Les URLs de streaming expirent après ~6h, le cache gère cela automatiquement

### Quotas de l'API
- L'API YouTube Data v3 a un quota de 10 000 unités/jour
- La recherche coûte 100 unités, les autres opérations 1-3 unités
- Pour un usage personnel, ce quota est largement suffisant
