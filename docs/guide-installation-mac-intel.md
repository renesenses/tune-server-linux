# Installer Tune Server sur Mac Intel

## Ce qu'il vous faut

- Un Mac Intel (2012 ou plus récent)
- macOS 13 (Ventura) ou plus récent
- Une connexion internet
- 10 minutes

---

## Étape 1 : Ouvrir le Terminal

1. Appuyez sur **Cmd + Espace** (la barre de recherche Spotlight apparaît)
2. Tapez **Terminal**
3. Appuyez sur **Entrée**

Une fenêtre noire (ou blanche) avec du texte apparaît. C'est le Terminal.
Vous allez copier-coller les commandes ci-dessous dedans.

---

## Étape 2 : Installer Homebrew (le gestionnaire de paquets)

Copiez cette commande et collez-la dans le Terminal, puis appuyez sur Entrée :

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

- Si on vous demande votre **mot de passe Mac**, tapez-le (rien ne s'affiche pendant la saisie, c'est normal) puis Entrée
- Si on vous demande d'appuyer sur **Entrée pour continuer**, faites-le
- L'installation prend 2-5 minutes

**Si Homebrew est déjà installé**, la commande vous le dira — pas de souci, passez à l'étape suivante.

---

## Étape 3 : Installer Tune Server

Copiez et collez cette commande dans le Terminal :

```
brew tap renesenses/tap && brew install renesenses/tap/tune-server
```

L'installation prend 3-5 minutes (téléchargement de Python, FFmpeg et Tune).

Quand c'est terminé, vous verrez un message qui commence par "Tune Server v0.7.86 installed!"

---

## Étape 4 : Lancer Tune Server

Copiez et collez dans le Terminal :

```
tune-server
```

Le serveur démarre. Après quelques secondes, vous verrez des lignes défiler.

Ouvrez votre navigateur (Safari, Chrome...) et allez sur :

**http://localhost:8888**

L'interface de Tune apparaît. Vous pouvez configurer vos dossiers de musique depuis Réglages.

---

## Lancement automatique au démarrage (optionnel)

Pour que Tune se lance tout seul quand vous allumez votre Mac :

```
brew services start renesenses/tap/tune-server
```

Pour arrêter le lancement automatique :

```
brew services stop renesenses/tap/tune-server
```

---

## Mettre à jour Tune

Quand une nouvelle version sort :

```
brew upgrade renesenses/tap/tune-server
```

---

## En cas de problème

### "Command not found: brew"

Après l'installation de Homebrew, fermez le Terminal et rouvrez-le.

### "Command not found: tune-server"

Fermez le Terminal, rouvrez-le, et réessayez. Si ça persiste :

```
brew link renesenses/tap/tune-server
```

### Le navigateur n'affiche rien sur localhost:8888

Attendez 10 secondes après avoir lancé `tune-server`, puis rechargez la page.

### "Xcode Command Line Tools" demandé

Cliquez **Installer** dans la fenêtre qui apparaît. Attendez la fin, puis relancez la commande.

### Tout désinstaller proprement

```
brew services stop renesenses/tap/tune-server
brew uninstall renesenses/tap/tune-server
brew untap renesenses/tap
```

---

## Résumé en 4 commandes

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew tap renesenses/tap && brew install renesenses/tap/tune-server
tune-server
```

Puis ouvrir **http://localhost:8888**
