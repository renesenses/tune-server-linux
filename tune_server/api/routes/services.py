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
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "text"},
            {"key": "api_secret", "label": "API Secret (optionnel, pour scrobbling)", "type": "password"},
        ],
        "help_url": "https://www.last.fm/api/account/create",
        "help_steps": [
            "Va sur https://www.last.fm/api/account/create",
            "Renseigne un nom d'application (ex: 'Tune Server') — le reste peut rester vide.",
            "Récupère 'API key' et (si tu veux scrobbler) 'Shared secret'.",
            "Colle les valeurs ici, puis Enregistrer.",
        ],
    },
    "tidal": {
        "name": "Tidal",
        "kind": "oauth",
        "purpose": "Streaming hi-res + années + couvertures.",
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


VALIDATORS = {
    "discogs": _validate_discogs,
    "lastfm": _validate_lastfm,
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
        out.append({
            "id": sid,
            "name": info["name"],
            "kind": info["kind"],
            "purpose": info["purpose"],
            "fields": info["fields"],
            "help_url": info["help_url"],
            "help_steps": info["help_steps"],
            "configured": configured,
            "source": "db" if payload else ("env" if env_fallback else None),
            "valid": payload.get("valid") if payload else None,
            "validated_at": payload.get("validated_at") if payload else None,
            "validation_message": payload.get("validation_message") if payload else None,
        })
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
