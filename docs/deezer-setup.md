# Configuration Deezer

Tune Server utilise l'API Deezer pour la recherche, la navigation d'albums/artistes et l'accès aux playlists. Chaque installation nécessite la création d'une application Deezer (gratuite) sur le portail développeur.

> **Note :** L'API Deezer ne fournit pas d'URLs de streaming audio complet. Seules les previews de 30 secondes sont disponibles via l'API, quel que soit l'abonnement. Un compte Deezer Free suffit pour la recherche et la navigation.

## Free vs Premium

| Fonctionnalité | Free | Premium |
|----------------|------|---------|
| Recherche (tracks, albums, artistes) | Oui | Oui |
| Navigation albums et artistes | Oui | Oui |
| Accès aux playlists personnelles | Oui | Oui |
| Previews 30 secondes | Oui | Oui |
| Streaming audio complet | Non | Non* |

\* L'API Deezer ne fournit **aucune URL de streaming audio complet**, quel que soit le type d'abonnement. Le streaming complet n'est possible qu'au travers des applications officielles Deezer. Un compte Premium n'apporte donc pas de fonctionnalité supplémentaire dans Tune Server.

> **En résumé :** Deezer dans Tune Server sert principalement de **catalogue de navigation et de découverte**. Il permet de chercher des morceaux, parcourir des albums et artistes, consulter ses playlists, puis éventuellement retrouver ces morceaux sur d'autres services (Tidal, Qobuz) pour une lecture en qualité complète.

## Différence avec Spotify

Deezer utilise un flux OAuth 2.0 **standard** (pas PKCE). Cela signifie qu'un **App Secret** est requis côté serveur, en plus de l'App ID.

## 1. Créer une application Deezer

1. Se rendre sur **https://developers.deezer.com/myapps**
2. Se connecter avec son compte Deezer
3. Cliquer sur **Create a new Application**
4. Remplir le formulaire :
   - **Application name** : `Tune Server` (ou un nom de votre choix)
   - **Application domain** : l'URL de votre serveur, par exemple `http://192.168.1.50:8888`
   - **Redirect URL after authentication** :
     ```
     http://192.168.1.50:8888/api/v1/streaming/deezer/callback
     ```
     Remplacer `192.168.1.50:8888` par l'adresse et le port de votre installation Tune Server.
5. Valider la création

## 2. Récupérer l'App ID et l'App Secret

1. Dans la page de votre application, noter :
   - **Application ID** (nombre)
   - **Secret Key** (chaîne de caractères)

> **Important :** Le Secret Key doit rester confidentiel. Ne le partagez pas et ne le commitez pas dans un dépôt public.

## 3. Configurer Tune Server

Ajouter les variables suivantes dans le fichier `.env` à la racine de Tune Server :

```env
TUNE_DEEZER_ENABLED=true
TUNE_DEEZER_APP_ID=votre_app_id_ici
TUNE_DEEZER_APP_SECRET=votre_secret_key_ici
TUNE_DEEZER_REDIRECT_URI=http://192.168.1.50:8888/api/v1/streaming/deezer/callback
```

| Variable | Description | Défaut |
|----------|-------------|--------|
| `TUNE_DEEZER_ENABLED` | Active le connecteur Deezer | `false` |
| `TUNE_DEEZER_APP_ID` | Application ID de votre application Deezer | _(requis)_ |
| `TUNE_DEEZER_APP_SECRET` | Secret Key de votre application Deezer | _(requis)_ |
| `TUNE_DEEZER_REDIRECT_URI` | URL de callback OAuth (doit correspondre exactement à celle déclarée dans le portail Deezer) | `http://localhost:8888/api/v1/streaming/deezer/callback` |

Redémarrer le serveur après modification :

```bash
sudo systemctl restart tune-server
```

## 4. Se connecter depuis l'interface

1. Ouvrir Tune Server dans le navigateur
2. Aller dans **Paramètres** > **Services de streaming**
3. Deezer apparaît dans la liste avec le statut "Non connecté"
4. Cliquer sur **Connecter à Deezer**
5. Un lien d'autorisation s'affiche : cliquer dessus
6. Se connecter sur Deezer et autoriser l'application
7. Deezer redirige vers Tune Server, la connexion est automatiquement validée
8. Le statut passe à "Connecté"

## Permissions demandées

L'application demande les permissions suivantes :
- **basic_access** : accès aux informations de base du profil
- **manage_library** : accès à la bibliothèque et aux playlists
- **listening_history** : accès à l'historique d'écoute

## Dépannage

### Le bouton Deezer n'apparaît pas dans les paramètres
- Vérifier que `TUNE_DEEZER_ENABLED=true` est bien dans le `.env`
- Vérifier que le serveur a été redémarré après la modification

### Erreur lors du callback ("Deezer not configured")
- L'URL de redirect dans le `.env` doit correspondre **exactement** à celle déclarée dans le portail Deezer (protocole, hôte, port, chemin)

### "wrong code" lors de l'authentification
- Le code d'autorisation peut expirer rapidement. Réessayer la connexion depuis les paramètres
- Vérifier que l'App Secret est correct

### Le token expire
- Les tokens Deezer n'expirent pas par défaut, mais peuvent être révoqués
- Les tokens sont automatiquement sauvegardés et restaurés au redémarrage
- Si la session ne fonctionne plus, cliquer sur "Déconnecter" puis se reconnecter
