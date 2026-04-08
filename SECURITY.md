# Tune Server - Sécurisation

## API Key

Tune Server supporte l'authentification par clé API. Quand elle est configurée, toutes les requêtes API et WebSocket doivent fournir la clé.

### Activation

Dans `.env` :
```
TUNE_API_KEY=votre-cle-secrete-ici
```

### Utilisation

- **API REST** : header `X-API-Key: votre-cle` ou query param `?api_key=votre-cle`
- **WebSocket** : query param `ws://host:8888/ws?api_key=votre-cle`
- **App iOS/macOS** : Réglages → Télécommande → Clé API

### Endpoints non protégés

- `GET /` — page d'accueil / web UI
- `GET /api/v1/system/health` — health check (monitoring)
- Assets statiques (CSS, JS, images du web client)

### Port 8080 (HTTP streaming)

Le port de streaming audio (DLNA) n'est **pas** protégé par l'API key car les renderers DLNA ne peuvent pas envoyer de headers d'authentification. Les URLs de stream contiennent des UUIDs éphémères qui ne sont valides que pendant la lecture.

## Déploiement sécurisé (bar, restaurant, lieu public)

### 1. Réseau (le plus important)

```
[Internet] ── Routeur ── WiFi Clients (isolé)
                 │
                 └── VLAN Audio ── Tune Server (.29)
                                   ├── Enceinte DLNA 1
                                   ├── Enceinte DLNA 2
                                   └── iPad contrôle
```

- **VLAN dédié** ou **WiFi séparé** pour Tune + enceintes DLNA
- Le WiFi clients ne doit **pas** pouvoir atteindre les ports 8888/8080
- L'iPad/iPhone de contrôle doit être sur le VLAN audio

### 2. Configuration serveur

```env
# .env
TUNE_API_KEY=une-cle-longue-et-aleatoire
TUNE_CORS_ORIGINS=["http://192.168.1.29:8888"]
TUNE_LOG_LEVEL=WARNING
```

### 3. Firewall (iptables / nftables)

```bash
# Autoriser uniquement le VLAN audio
iptables -A INPUT -p tcp --dport 8888 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 8080 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 8888 -j DROP
iptables -A INPUT -p tcp --dport 8080 -j DROP
```

### 4. Checklist

- [ ] API key configurée dans `.env`
- [ ] API key saisie dans l'app iOS (Réglages → Clé API)
- [ ] CORS restreint aux IPs du réseau audio
- [ ] Réseau audio isolé du WiFi clients
- [ ] Firewall configuré sur le serveur
- [ ] Accès SSH sécurisé (clé, pas de password)
