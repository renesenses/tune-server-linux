## TUNE v0.1.5 est disponible !

Salut à tous,

La v0.1.5 de TUNE Server vient d'être publiée avec pas mal de nouveautés et de corrections importantes.

### Ce qui change pour vous

**Audio** — Le Micromega M-One est maintenant pleinement supporté : volume natif, DSD passthrough, et proxy automatique pour les flux Tidal/Qobuz. Si vous avez un M-One, tout devrait fonctionner out-of-the-box.

**Multi-room** — La synchronisation a été retravaillée. Le polling est adaptatif (plus rapide quand ça joue, économe au repos), et vous pouvez ajuster un offset par zone (`sync_delay_ms`) pour compenser les différences de latence entre vos appareils.

**Bibliothèque** — Vous pouvez maintenant éditer les tags de vos morceaux et albums directement depuis l'interface. Les métadonnées sont écrites dans les fichiers audio (FLAC, MP3, M4A, OGG). Les dossiers musique peuvent être ajoutés ou retirés à chaud, sans redémarrer le serveur.

**Web client** — Nouveau logo Tune dans la sidebar, un indicateur visuel (barres animées) quand un album est en lecture, et les noms d'artistes sont maintenant cliquables y compris pour les sources streaming.

**Bug fix important** — Un problème de buffer non aligné faisait sauter tous les morceaux dans certaines configurations. C'est corrigé.

### Comment mettre à jour

Téléchargez la version pour votre système sur [mozaiklabs.fr/download](https://mozaiklabs.fr/download) :
- **macOS** (Apple Silicon et Intel)
- **Linux** (Debian/Ubuntu)
- **Windows**

Ou directement sur [GitHub](https://github.com/renesenses/tune-server-linux/releases/tag/v0.1.5).

Pour mettre à jour une installation existante :
```bash
# Linux/macOS
cd /opt/tune-server
git pull
pip install -e .
sudo systemctl restart tune-server
```

### Vos retours

N'hésitez pas à poster vos retours ici ou à ouvrir un sujet dans la section **Bug report** si vous rencontrez un problème. Mentionnez votre OS, votre matériel audio, et les étapes pour reproduire.

Bonne écoute !
