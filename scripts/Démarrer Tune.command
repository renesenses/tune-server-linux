#!/bin/bash
# Tune Server — lanceur direct (double-cliquez ce fichier)
# Fonctionne sans l'icône menubar, ouvre le navigateur automatiquement.

APP_DIR="/Applications/Tune Server.app/Contents/Resources/runtime"
DATA_DIR="$HOME/Library/Application Support/Tune Server"

mkdir -p "$DATA_DIR"

export TUNE_DB_PATH="$DATA_DIR/tune_server.db"
export TUNE_ARTWORK_CACHE_DIR="$DATA_DIR/artwork_cache"

echo "============================================"
echo "  Tune Server"
echo "============================================"
echo ""
echo "  Démarrage en cours..."
echo "  Ne fermez pas cette fenêtre."
echo ""

if [ ! -f "$APP_DIR/tune-server" ]; then
    echo "ERREUR: tune-server introuvable dans $APP_DIR"
    echo "Vérifiez que Tune Server.app est installé dans /Applications."
    read -p "Appuyez sur Entrée pour fermer..."
    exit 1
fi

cd "$DATA_DIR"

# Ouvrir le navigateur après 4 secondes
(sleep 4 && open "http://localhost:8888") &

"$APP_DIR/tune-server"
