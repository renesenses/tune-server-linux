# Configuration Spotify

Tune Server utilise l'API Web Spotify pour la recherche, la navigation d'albums/artistes et l'accès aux playlists. Chaque installation nécessite la création d'une application Spotify (gratuite) sur le portail développeur.

> **Note :** L'API Web Spotify ne fournit pas d'URLs de streaming audio complet. Seules les previews de 30 secondes sont disponibles via l'API. Un compte Spotify Free suffit pour la recherche et la navigation.

## Free vs Premium

| Fonctionnalité | Free | Premium |
|----------------|------|---------|
| Recherche (tracks, albums, artistes) | Oui | Oui |
| Navigation albums et artistes | Oui | Oui |
| Accès aux playlists personnelles | Oui | Oui |
| Playlists collaboratives | Oui | Oui |
| Previews 30 secondes | Oui | Oui |
| Streaming audio complet | Non | Non* |
| Recommendations / Discover Weekly | Oui | Oui |

\* L'API Web Spotify ne fournit **aucune URL de streaming audio complet**, quel que soit le type d'abonnement. C'est une limitation imposée par Spotify : le streaming complet n'est possible qu'au travers de leurs applications officielles ou du SDK Playback (réservé aux applications approuvées). Un compte Premium n'apporte donc pas de fonctionnalité supplémentaire dans Tune Server.

> **En résumé :** Spotify dans Tune Server sert principalement de **catalogue de navigation et de découverte**. Il permet de chercher des morceaux, parcourir des albums et artistes, consulter ses playlists, puis éventuellement retrouver ces morceaux sur d'autres services (Tidal, Qobuz) pour une lecture en qualité complète.

## 1. Créer une application Spotify

1. Se rendre sur **https://developer.spotify.com/dashboard**
2. Se connecter avec son compte Spotify (Free ou Premium)
3. Cliquer sur **Create app**
4. Remplir le formulaire :
   - **App name** : `Tune Server` (ou un nom de votre choix)
   - **App description** : `Music server integration`
   - **Redirect URIs** : ajouter l'URL de callback de votre serveur, par exemple :
     ```
     http://192.168.1.50:8888/api/v1/streaming/spotify/callback
     ```
     Remplacer `192.168.1.50:8888` par l'adresse et le port de votre installation Tune Server.
   - **Which API/SDKs are you planning to use?** : cocher **Web API**
5. Cocher la case d'acceptation des conditions d'utilisation
6. Cliquer sur **Save**

## 2. Récupérer le Client ID

1. Dans le dashboard de l'application, aller dans **Settings**
2. Copier le **Client ID** (une chaîne de 32 caractères hexadécimaux)

> Pas besoin du Client Secret : Tune Server utilise le flux OAuth PKCE qui n'en a pas besoin.

## 3. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_SPOTIFY_ENABLED=true
TUNE_SPOTIFY_CLIENT_ID=votre_client_id_ici
TUNE_SPOTIFY_REDIRECT_URI=http://192.168.1.50:8888/api/v1/streaming/spotify/callback
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_SPOTIFY_ENABLED` | Active le connecteur Spotify | `false` |
| `TUNE_SPOTIFY_CLIENT_ID` | Client ID de votre application Spotify | _(requis)_ |
| `TUNE_SPOTIFY_REDIRECT_URI` | URL de callback OAuth (doit correspondre exactement à celle déclarée dans le dashboard Spotify) | `http://localhost:8888/api/v1/streaming/spotify/callback` |

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 4. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. Spotify apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à Spotify**
5. Un lien d'autorisation s'affiche : cliquer dessus
6. Se connecter sur Spotify et autoriser l'application
7. Spotify redirige vers Tune Server, la connexion est automatiquement validée
8. Le statut passe à "Connecté"

## Dépannage

### Le bouton Spotify n'apparaît pas dans les paramètres
- Vérifier que `TUNE_SPOTIFY_ENABLED=true` est bien dans le `.env`
- Vérifier que le serveur a été redémarré après la modification

### Erreur lors du callback ("Spotify not configured")
- L'URL de redirect dans le `.env` doit correspondre **exactement** à celle déclarée dans le dashboard Spotify (protocole, hôte, port, chemin)

### Le token expire
- Les tokens sont automatiquement sauvegardés et restaurés au redémarrage
- Si la session expire, cliquer sur "Déconnecter" puis se reconnecter

### Quotas de l'API
- Les applications Spotify en mode développement sont limitées à 25 utilisateurs
- Pour un usage personnel, cette limite est largement suffisante
