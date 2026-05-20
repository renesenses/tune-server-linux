"""Service tokens — self-service per-user token management for MusicBrainz,
Discogs, Last.fm and other metadata providers.

Tokens are stored in `streaming_auth` (re-using the existing per-service
table). Each row's `token_data` is a JSON payload with the secret(s) plus
a `validated_at` timestamp and `valid` boolean. Read paths in
`metadata/suggestions.py` prefer the DB-stored value over the env var,
so users can self-serve without editing `.env` or restarting.
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps
from tune_server.config import settings

router = APIRouter(prefix="/services", tags=["services"])


# Catalog of services we know how to surface in the UI.
# Each entry tells the frontend what fields to ask for and the validator
# tells the backend how to test the credential.
SERVICE_CATALOG: dict[str, dict] = {
    "musicbrainz": {
        "name": "MusicBrainz",
        "kind": "no_auth",
        "purpose": "Années + crédits + couvertures (ID releases).",
        "pricing": "free",
        "pricing_note": "100 % gratuit, base de données ouverte.",
        "fields": [],
        "help_url": "https://musicbrainz.org/",
        "help_steps": [
            "Aucun token requis — MusicBrainz est gratuit et anonyme.",
            "Tune utilise un User-Agent identifiant le serveur, ce qui suffit pour respecter les quotas.",
        ],
    },
    "discogs": {
        "name": "Discogs",
        "kind": "personal_token",
        "purpose": "Années + couvertures + crédits pour pressages obscurs.",
        "pricing": "free",
        "pricing_note": "Compte + token personnel gratuits ; API gratuite avec quota (60 req/min authentifié).",
        "fields": [
            {"key": "token", "label": "Personal Access Token", "type": "password"},
        ],
        "help_url": "https://www.discogs.com/settings/developers",
        "help_steps": [
            "Connecte-toi sur discogs.com.",
            "Va dans Settings → Developers : https://www.discogs.com/settings/developers",
            "Clique 'Generate new token' (Personal Access Token).",
            "Copie le token et colle-le ici, puis Enregistrer.",
        ],
    },
    "lastfm": {
        "name": "Last.fm",
        "kind": "api_key",
        "purpose": "Genres + scrobbling.",
        "pricing": "free",
        "pricing_note": "API gratuite pour usage non commercial. Scrobbling = lecture suivie sur Last.fm — gratuit aussi.",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "text"},
            {"key": "api_secret", "label": "API Secret (pour scrobbling)", "type": "password"},
        ],
        "help_url": "https://www.last.fm/api/account/create",
        "help_steps": [
            "Va sur https://www.last.fm/api/account/create",
            "Renseigne un nom d'application (ex: 'Tune Server') — le reste peut rester vide.",
            "Récupère 'API key' et (si tu veux scrobbler) 'Shared secret'.",
            "Colle les valeurs ici, puis Enregistrer.",
            "Ensuite, clique 'Connecter mon compte Last.fm' pour autoriser le scrobbling.",
        ],
    },
    "tidal": {
        "name": "Tidal",
        "kind": "oauth",
        "purpose": "Streaming hi-res + années + couvertures.",
        "pricing": "paid",
        "pricing_note": "Abonnement Tidal HiFi requis (≈ 11€/mois HiFi, 22€/mois HiFi Plus).",
        "fields": [],
        "help_url": "/streaming/tidal",
        "help_steps": [
            "Tidal utilise OAuth — utilise la page Streaming → Tidal pour te connecter.",
        ],
    },
    "qobuz": {
        "name": "Qobuz",
        "kind": "login_password",
        "purpose": "Streaming hi-res + années + couvertures.",
        "pricing": "paid",
        "pricing_note": "Abonnement Qobuz Studio requis (≈ 13€/mois Studio, 22€/mois Sublime).",
        "fields": [],
        "help_url": "/streaming/qobuz",
        "help_steps": [
            "Qobuz utilise login/password — utilise la page Streaming → Qobuz pour te connecter.",
        ],
    },
    "spotify": {
        "name": "Spotify",
        "kind": "oauth",
        "purpose": "Streaming + connectivité.",
        "pricing": "freemium",
        "pricing_note": "Compte Spotify gratuit (avec pubs) ou Premium (≈ 11€/mois) requis pour Spotify Connect haute qualité.",
        "fields": [],
        "help_url": "/streaming/spotify",
        "help_steps": [
            "Spotify utilise OAuth — utilise la page Streaming → Spotify pour te connecter.",
        ],
    },
    "deezer": {
        "name": "Deezer",
        "kind": "arl_token",
        "purpose": "Streaming.",
        "pricing": "freemium",
        "pricing_note": "Compte gratuit (avec pubs) ou Deezer HiFi (≈ 12€/mois) pour FLAC.",
        "fields": [
            {"key": "arl", "label": "ARL token (depuis cookies deezer.com)", "type": "password"},
        ],
        "help_url": "/streaming/deezer",
        "help_steps": [
            "Connecte-toi sur deezer.com.",
            "DevTools (F12) → Application → Cookies → cherche 'arl' → copie la valeur.",
            "Ou utilise une extension comme 'EditThisCookie' pour exporter les cookies.",
            "Colle le token ARL ici, puis Enregistrer.",
        ],
    },
}


# ---------------------------------------------------------------------------
# Token storage — backed by streaming_auth(service PK, token_data JSON).
# ---------------------------------------------------------------------------

async def _load_token(service: str) -> dict | None:
    """Return the stored token payload (dict) for service, or None if absent."""
    row = await deps.db.fetchone(
        "SELECT token_data FROM streaming_auth WHERE service = ?",
        (service,),
    )
    if not row:
        return None
    try:
        return json.loads(row["token_data"])
    except Exception:
        return None


async def _save_token(service: str, payload: dict) -> None:
    """Upsert the token payload."""
    blob = json.dumps(payload)
    # SQLite + PostgreSQL compatible upsert via DELETE + INSERT.
    await deps.db.execute("DELETE FROM streaming_auth WHERE service = ?", (service,))
    await deps.db.execute(
        "INSERT INTO streaming_auth (service, token_data) VALUES (?, ?)",
        (service, blob),
    )
    await deps.db.commit()


async def _delete_token(service: str) -> None:
    await deps.db.execute("DELETE FROM streaming_auth WHERE service = ?", (service,))
    await deps.db.commit()


# Convenience read helper that the metadata / streaming routes can call to
# prefer DB-stored credentials over env-loaded settings. Returns the token
# string for "single-secret" services or a dict for multi-field services.
async def get_credential(service: str, key: str = "token") -> str | None:
    payload = await _load_token(service)
    if not payload:
        return None
    val = payload.get(key)
    return val if isinstance(val, str) and val else None


async def get_discogs_token() -> str:
    """Return the Discogs token: .env first, then DB (Services UI) fallback."""
    if settings.discogs_token:
        return settings.discogs_token
    return await get_credential("discogs", "token") or ""


# ---------------------------------------------------------------------------
# Validators — one per service that has a network-testable credential.
# ---------------------------------------------------------------------------

async def _validate_discogs(payload: dict) -> tuple[bool, str]:
    token = payload.get("token", "").strip()
    if not token:
        return False, "Token vide."
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "TuneServer/0.7.x +https://mozaiklabs.fr",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.discogs.com/oauth/identity",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return True, f"Token valide (utilisateur: {data.get('username', '?')})."
                if resp.status == 401:
                    return False, "Token invalide (401)."
                return False, f"HTTP {resp.status} — vérifie le token."
    except asyncio.TimeoutError:
        return False, "Timeout (>8s) sur api.discogs.com."
    except Exception as e:
        return False, f"Erreur: {e}"


async def _validate_lastfm(payload: dict) -> tuple[bool, str]:
    api_key = payload.get("api_key", "").strip()
    if not api_key:
        return False, "API Key vide."
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "auth.getToken",
                    "api_key": api_key,
                    "format": "json",
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("error"):
                        return False, f"Last.fm: {data.get('message', 'erreur inconnue')}"
                    if "token" in data:
                        return True, "API Key valide."
                    return False, "Réponse inattendue de Last.fm."
                return False, f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        return False, "Timeout (>8s) sur Last.fm."
    except Exception as e:
        return False, f"Erreur: {e}"


async def _validate_deezer(payload: dict) -> tuple[bool, str]:
    """Validate a Deezer ARL by calling the gateway API and, on success,
    trigger the streaming connector auth so the user can stream immediately."""
    arl = payload.get("arl", "").strip()
    if not arl:
        return False, "Token ARL vide."
    if len(arl) < 100:
        return False, (
            "Token ARL trop court — il doit faire ~192 caracteres. "
            "Verifiez que vous avez copie la valeur complete."
        )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            query = {
                "method": "deezer.getUserData",
                "input": "3",
                "api_version": "1.0",
                "api_token": "",
            }
            async with s.post(
                "https://www.deezer.com/ajax/gw-light.php",
                params=query,
                json={},
                cookies={"arl": arl},
            ) as resp:
                if resp.status != 200:
                    return False, f"Deezer a repondu HTTP {resp.status}."
                data = await resp.json()
                results = data.get("results", {})
                user = results.get("USER", {})
                user_id = str(user.get("USER_ID", ""))
                if not user_id or user_id == "0":
                    return False, (
                        "Token ARL invalide ou expire. "
                        "Reconnectez-vous sur deezer.com et copiez un nouveau cookie 'arl'."
                    )
                user_name = user.get("BLOG_NAME", user_id)
                has_license = bool(user.get("OPTIONS", {}).get("license_token"))
    except asyncio.TimeoutError:
        return False, "Timeout (>15s) sur deezer.com."
    except Exception as e:
        return False, f"Erreur: {e}"

    # ARL is valid — now trigger the streaming connector auth so the user
    # can stream immediately without restarting the server.
    svc = deps.streaming_services.get("deezer")
    if svc:
        try:
            ok = await svc.authenticate(arl=arl)
            if ok and deps.db:
                await svc.save_auth(deps.db)
        except Exception:
            pass  # non-critical — the ARL itself is valid

    quality = " (FLAC)" if has_license else " (previews 30s — abonnement requis pour FLAC)"
    return True, f"Token valide — utilisateur: {user_name}{quality}."


VALIDATORS = {
    "discogs": _validate_discogs,
    "lastfm": _validate_lastfm,
    "deezer": _validate_deezer,
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/tokens")
async def list_service_tokens():
    """Return the catalog plus the saved state for each service."""
    out = []
    for sid, info in SERVICE_CATALOG.items():
        payload = await _load_token(sid)
        env_fallback = _env_credential(sid)  # for legacy users who haven't migrated yet
        configured = bool(payload) or bool(env_fallback)
        entry = {
            "id": sid,
            "name": info["name"],
            "kind": info["kind"],
            "purpose": info["purpose"],
            "pricing": info.get("pricing", "unknown"),
            "pricing_note": info.get("pricing_note", ""),
            "fields": info["fields"],
            "help_url": info["help_url"],
            "help_steps": info["help_steps"],
            "configured": configured,
            "source": "db" if payload else ("env" if env_fallback else None),
            "valid": payload.get("valid") if payload else None,
            "validated_at": payload.get("validated_at") if payload else None,
            "validation_message": payload.get("validation_message") if payload else None,
        }
        # Extra Last.fm scrobbling fields
        if sid == "lastfm":
            has_sk = bool((payload or {}).get("session_key")) or bool(
                getattr(settings, "lastfm_session_key", "")
            )
            entry["scrobble_enabled"] = (payload or {}).get(
                "scrobble_enabled", getattr(settings, "lastfm_scrobble_enabled", False)
            )
            entry["scrobble_authenticated"] = has_sk
            entry["lastfm_username"] = (payload or {}).get("lastfm_username", "") or ""
        out.append(entry)
    return out


def _env_credential(service: str) -> str | None:
    """Look at config.settings to surface env-configured tokens."""
    if service == "discogs":
        return getattr(settings, "discogs_token", None) or None
    if service == "lastfm":
        return getattr(settings, "lastfm_api_key", None) or None
    return None


@router.post("/tokens/{service}")
async def save_service_token(service: str, body: dict):
    """Save a token payload + validate it. Returns the validation result."""
    if service not in SERVICE_CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    info = SERVICE_CATALOG[service]
    if info["kind"] == "no_auth":
        raise HTTPException(status_code=400, detail="Ce service n'a pas de token à configurer.")
    if info["kind"] in ("oauth", "login_password"):
        raise HTTPException(
            status_code=400,
            detail=f"Ce service utilise {info['kind']} — utilise la page dédiée.",
        )

    # Sanitize: only keep declared fields.
    declared = {f["key"] for f in info["fields"]}
    payload = {k: (v or "").strip() for k, v in body.items() if k in declared and isinstance(v, str)}
    if not payload:
        raise HTTPException(status_code=400, detail="Aucun champ valide reçu.")

    # Validate (if a validator exists for this service).
    valid: bool | None = None
    msg = ""
    validator = VALIDATORS.get(service)
    if validator:
        valid, msg = await validator(payload)

    payload["valid"] = valid
    payload["validation_message"] = msg
    payload["validated_at"] = int(time.time())

    await _save_token(service, payload)
    return {
        "ok": True,
        "service": service,
        "valid": valid,
        "validation_message": msg,
    }


@router.post("/tokens/{service}/test")
async def test_service_token(service: str):
    """Re-run the validator against the currently stored token."""
    if service not in SERVICE_CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    payload = await _load_token(service)
    if not payload:
        raise HTTPException(status_code=404, detail="Aucun token enregistré pour ce service.")
    validator = VALIDATORS.get(service)
    if not validator:
        return {"ok": True, "service": service, "valid": None,
                "validation_message": "Pas de validation disponible."}
    valid, msg = await validator(payload)
    payload["valid"] = valid
    payload["validation_message"] = msg
    payload["validated_at"] = int(time.time())
    await _save_token(service, payload)
    return {"ok": True, "service": service, "valid": valid, "validation_message": msg}


@router.delete("/tokens/{service}")
async def delete_service_token(service: str):
    if service not in SERVICE_CATALOG:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    await _delete_token(service)
    return {"ok": True, "service": service}


# ---------------------------------------------------------------------------
# Last.fm scrobbling — auth flow + toggle
# ---------------------------------------------------------------------------

@router.get("/lastfm/status")
async def lastfm_scrobble_status():
    """Return the current Last.fm scrobbling status."""
    payload = await _load_token("lastfm")
    api_key = (payload or {}).get("api_key", "").strip() if payload else ""
    api_secret = (payload or {}).get("api_secret", "").strip() if payload else ""
    session_key = (payload or {}).get("session_key", "").strip() if payload else ""
    scrobble_enabled = (payload or {}).get("scrobble_enabled", False) if payload else False
    lastfm_username = (payload or {}).get("lastfm_username", "") if payload else ""

    # Fallback to env vars
    if not api_key:
        api_key = getattr(settings, "lastfm_api_key", "") or ""
    if not api_secret:
        api_secret = getattr(settings, "lastfm_api_secret", "") or ""
    if not session_key:
        session_key = getattr(settings, "lastfm_session_key", "") or ""
    if not scrobble_enabled:
        scrobble_enabled = getattr(settings, "lastfm_scrobble_enabled", False)

    return {
        "has_api_key": bool(api_key),
        "has_api_secret": bool(api_secret),
        "has_session_key": bool(session_key),
        "scrobble_enabled": scrobble_enabled,
        "authenticated": bool(session_key),
        "username": lastfm_username or None,
    }


@router.post("/lastfm/auth/token")
async def lastfm_get_auth_token():
    """Step 1: Get a request token from Last.fm and return the auth URL."""
    payload = await _load_token("lastfm")
    api_key = (payload or {}).get("api_key", "").strip() if payload else ""
    api_secret = (payload or {}).get("api_secret", "").strip() if payload else ""

    # Fallback to env
    if not api_key:
        api_key = getattr(settings, "lastfm_api_key", "") or ""
    if not api_secret:
        api_secret = getattr(settings, "lastfm_api_secret", "") or ""

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="API Key et API Secret requis pour l'authentification Last.fm.",
        )

    from tune_server.metadata.lastfm_scrobbler import LastfmScrobbler
    scrobbler = LastfmScrobbler(api_key=api_key, api_secret=api_secret)
    token = await scrobbler.get_auth_token()
    if not token:
        raise HTTPException(status_code=502, detail="Impossible d'obtenir un token Last.fm.")

    auth_url = scrobbler.get_auth_url(token)
    return {"token": token, "auth_url": auth_url}


@router.post("/lastfm/auth/session")
async def lastfm_get_session(body: dict):
    """Step 2: Exchange the authorized token for a session key."""
    token = body.get("token", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token manquant.")

    payload = await _load_token("lastfm")
    api_key = (payload or {}).get("api_key", "").strip() if payload else ""
    api_secret = (payload or {}).get("api_secret", "").strip() if payload else ""

    if not api_key:
        api_key = getattr(settings, "lastfm_api_key", "") or ""
    if not api_secret:
        api_secret = getattr(settings, "lastfm_api_secret", "") or ""

    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="API Key et Secret requis.")

    from tune_server.metadata.lastfm_scrobbler import LastfmScrobbler
    scrobbler = LastfmScrobbler(api_key=api_key, api_secret=api_secret)
    session_key = await scrobbler.get_session(token)
    if not session_key:
        raise HTTPException(
            status_code=400,
            detail="Impossible d'obtenir la session — as-tu bien autorisé l'app sur Last.fm ?",
        )

    # Fetch the username for display
    username = ""
    try:
        async with aiohttp.ClientSession() as s:
            import hashlib
            params = {
                "method": "user.getInfo",
                "api_key": api_key,
                "sk": session_key,
            }
            sig_str = "".join(f"{k}{params[k]}" for k in sorted(params)) + api_secret
            api_sig = hashlib.md5(sig_str.encode()).hexdigest()
            params["api_sig"] = api_sig
            params["format"] = "json"
            async with s.get("https://ws.audioscrobbler.com/2.0/", params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    username = data.get("user", {}).get("name", "")
    except Exception:
        pass  # non-critical

    # Persist session key into the token payload
    if not payload:
        payload = {"api_key": api_key, "api_secret": api_secret}
    payload["session_key"] = session_key
    payload["scrobble_enabled"] = True
    payload["lastfm_username"] = username
    payload["valid"] = True
    payload["validation_message"] = f"Connecté à Last.fm{f' ({username})' if username else ''} — scrobbling activé."
    payload["validated_at"] = int(time.time())
    await _save_token("lastfm", payload)

    return {
        "ok": True,
        "session_key": session_key,
        "username": username,
        "scrobble_enabled": True,
    }


@router.post("/lastfm/scrobble/toggle")
async def lastfm_toggle_scrobble(body: dict):
    """Enable or disable scrobbling."""
    enabled = body.get("enabled", False)
    payload = await _load_token("lastfm")
    if not payload:
        raise HTTPException(status_code=404, detail="Aucun token Last.fm enregistré.")

    payload["scrobble_enabled"] = bool(enabled)
    if enabled and not payload.get("session_key"):
        raise HTTPException(
            status_code=400,
            detail="Session key manquante — connecte d'abord ton compte Last.fm.",
        )
    username = payload.get("lastfm_username", "")
    suffix = f" ({username})" if username else ""
    payload["validation_message"] = f"Scrobbling {'activé' if enabled else 'désactivé'}{suffix}."
    payload["validated_at"] = int(time.time())
    await _save_token("lastfm", payload)
    return {"ok": True, "scrobble_enabled": enabled}


@router.post("/lastfm/disconnect")
async def lastfm_disconnect():
    """Remove session key (disconnect Last.fm scrobbling account)."""
    payload = await _load_token("lastfm")
    if not payload:
        raise HTTPException(status_code=404, detail="Aucun token Last.fm enregistré.")

    payload.pop("session_key", None)
    payload.pop("lastfm_username", None)
    payload["scrobble_enabled"] = False
    payload["validation_message"] = "Compte Last.fm déconnecté."
    payload["validated_at"] = int(time.time())
    await _save_token("lastfm", payload)
    return {"ok": True}
