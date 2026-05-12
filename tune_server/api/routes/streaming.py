from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from tune_server.api.deps import deps
from tune_server.models import (
    Album,
    Artist,
    FeaturedSection,
    SearchResult,
    StreamingAuthRequest,
    StreamingAuthResponse,
    StreamingPlaylist,
    StreamingServiceStatus,
    Track,
)
from tune_server.streaming.base import StreamingService

router = APIRouter(prefix="/streaming", tags=["streaming"])


def _get_service(service_name: str) -> StreamingService:
    service = deps.streaming_services.get(service_name)
    if not service:
        raise HTTPException(status_code=503, detail=f"{service_name} not configured")
    if not service.is_authenticated:
        raise HTTPException(status_code=503, detail=f"{service_name} not authenticated")
    return service


_ALL_SERVICES = ["tidal", "qobuz", "youtube", "spotify", "deezer", "amazon"]


@router.get("/services", response_model=dict[str, StreamingServiceStatus])
async def list_services():
    result = {}
    for name in _ALL_SERVICES:
        service = deps.streaming_services.get(name)
        result[name] = StreamingServiceStatus(
            enabled=service is not None,
            authenticated=service.is_authenticated if service else False,
            iframe_only=getattr(service, "iframe_only", False) if service else False,
        )
    return result


@router.get("/{service_name}/status", response_model=StreamingServiceStatus)
async def service_status(service_name: str):
    service = deps.streaming_services.get(service_name)
    return StreamingServiceStatus(
        enabled=service is not None,
        authenticated=service.is_authenticated if service else False,
        iframe_only=getattr(service, "iframe_only", False) if service else False,
    )


@router.post("/{service_name}/enable")
async def enable_service(service_name: str):
    """Enable a streaming service and persist to .env."""
    if service_name not in _ALL_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_name}")
    if service_name in deps.streaming_services:
        return {"status": "already_enabled"}

    from tune_server.config import persist_env_var, settings
    env_key = f"TUNE_{service_name.upper()}_ENABLED"
    persist_env_var(env_key, "true")

    # Persist service-specific config to .env
    _SERVICE_ENV_DEFAULTS: dict[str, dict[str, str]] = {
        "tidal": {
            "TUNE_TIDAL_QUALITY": settings.tidal_quality or "HI_RES_LOSSLESS",
        },
        "qobuz": {
            "TUNE_QOBUZ_APP_ID": settings.qobuz_app_id or "798273057",
            "TUNE_QOBUZ_APP_SECRET": settings.qobuz_app_secret or "05a4851e74ee47fda346f50cfdfc4f09",
        },
        "spotify": {
            "TUNE_SPOTIFY_REDIRECT_URI": settings.spotify_redirect_uri,
        },
        "deezer": {
            "TUNE_DEEZER_QUALITY": settings.deezer_quality or "FLAC",
        },
        "amazon": {
            "TUNE_AMAZON_MUSIC_REGION": settings.amazon_music_region or "us",
            "TUNE_AMAZON_MUSIC_QUALITY": settings.amazon_music_quality or "HD",
        },
    }
    for key, value in _SERVICE_ENV_DEFAULTS.get(service_name, {}).items():
        persist_env_var(key, value)

    # Dynamically start the service
    _SERVICE_FACTORIES = {
        "tidal": lambda: __import__("tune_server.streaming.tidal", fromlist=["TidalService"]).TidalService(),
        "qobuz": lambda: __import__("tune_server.streaming.qobuz", fromlist=["QobuzService"]).QobuzService(),
        "youtube": lambda: __import__("tune_server.streaming.youtube", fromlist=["YouTubeService"]).YouTubeService(),
        "spotify": lambda: __import__("tune_server.streaming.spotify", fromlist=["SpotifyService"]).SpotifyService(),
        "deezer": lambda: __import__("tune_server.streaming.deezer", fromlist=["DeezerService"]).DeezerService(
            arl=settings.deezer_arl, quality=settings.deezer_quality,
        ),
        "amazon": lambda: __import__("tune_server.streaming.amazon", fromlist=["AmazonMusicService"]).AmazonMusicService(),
    }
    factory = _SERVICE_FACTORIES.get(service_name)
    if factory:
        try:
            service = factory()
            deps.streaming_services[service_name] = service
            # Try to restore auth from DB
            if deps.db:
                try:
                    await service.restore_auth(deps.db)
                except Exception:
                    pass
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to start {service_name}: {e}")

    return {"status": "enabled"}


@router.post("/{service_name}/disable")
async def disable_service(service_name: str):
    """Disable a streaming service and persist to .env."""
    if service_name not in _ALL_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_name}")

    from tune_server.config import persist_env_var
    env_key = f"TUNE_{service_name.upper()}_ENABLED"
    persist_env_var(env_key, "false")

    # Remove the running service instance
    if service_name in deps.streaming_services:
        service = deps.streaming_services.pop(service_name)
        if deps.db and hasattr(service, "disconnect"):
            try:
                await service.disconnect(deps.db)
            except Exception:
                pass

    return {"status": "disabled"}


@router.post("/{service_name}/auth", response_model=StreamingAuthResponse)
async def authenticate(
    service_name: str,
    request: StreamingAuthRequest | None = None,
):
    service = deps.streaming_services.get(service_name)
    if not service:
        raise HTTPException(status_code=503, detail=f"{service_name} not configured")
    kwargs = {}
    if request:
        if request.username is not None:
            kwargs["username"] = request.username
        if request.password is not None:
            kwargs["password"] = request.password
        if request.oauth_json is not None:
            kwargs["oauth_json"] = request.oauth_json
    success = await service.authenticate(**kwargs, db=deps.db)
    verification_url = getattr(service, "verification_url", None)
    user_code = getattr(service, "user_code", None)
    error = getattr(service, "_auth_error", None)
    if success and deps.db:
        await service.save_auth(deps.db)
    return StreamingAuthResponse(authenticated=success, verification_url=verification_url, user_code=user_code, error=error)


@router.post("/{service_name}/disconnect")
async def disconnect_service(service_name: str):
    service = deps.streaming_services.get(service_name)
    if not service:
        raise HTTPException(status_code=503, detail=f"{service_name} not configured")
    await service.disconnect(deps.db)
    return {"disconnected": True}


@router.get("/{service_name}/featured/sections", response_model=list[FeaturedSection])
async def get_featured_sections(service_name: str):
    service = _get_service(service_name)
    return await service.get_featured_sections()


@router.get("/{service_name}/featured/{section}", response_model=list[Album])
async def get_featured(service_name: str, section: str, limit: int = Query(20, ge=1, le=100)):
    service = _get_service(service_name)
    return await service.get_featured(section, limit=limit)


@router.get("/{service_name}/search", response_model=SearchResult)
async def search(service_name: str, q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200)):
    service = _get_service(service_name)
    return await service.search(q, limit=limit)


@router.get("/{service_name}/tracks/{track_id}", response_model=Track)
async def get_track(service_name: str, track_id: str):
    service = _get_service(service_name)
    track = await service.get_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    return track


@router.get("/{service_name}/albums/{album_id}", response_model=Album)
async def get_album(service_name: str, album_id: str):
    service = _get_service(service_name)
    album = await service.get_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    return album


@router.get("/{service_name}/albums/{album_id}/tracks", response_model=list[Track])
async def get_album_tracks(service_name: str, album_id: str):
    service = _get_service(service_name)
    return await service.get_album_tracks(album_id)


@router.get("/{service_name}/artists/{artist_id}", response_model=Artist)
async def get_artist(service_name: str, artist_id: str):
    service = _get_service(service_name)
    artist = await service.get_artist(artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    return artist


@router.get("/{service_name}/artists/{artist_id}/albums", response_model=list[Album])
async def get_artist_albums(service_name: str, artist_id: str):
    service = _get_service(service_name)
    return await service.get_artist_albums(artist_id)


@router.get("/{service_name}/artists/{artist_id}/tracks", response_model=list[Track])
async def get_artist_tracks(service_name: str, artist_id: str):
    service = _get_service(service_name)
    return await service.get_artist_tracks(artist_id)


@router.get("/{service_name}/playlists", response_model=list[StreamingPlaylist])
async def get_user_playlists(service_name: str):
    service = _get_service(service_name)
    return await service.get_user_playlists()


@router.get("/{service_name}/playlists/{playlist_id}/tracks", response_model=list[Track])
async def get_playlist_tracks(service_name: str, playlist_id: str):
    service = _get_service(service_name)
    return await service.get_playlist_tracks(playlist_id)


@router.get("/{service_name}/favorites/{fav_type}")
async def get_user_favorites(service_name: str, fav_type: str):
    service = _get_service(service_name)
    if not hasattr(service, "get_user_favorites"):
        return {}
    return await service.get_user_favorites(fav_type)


@router.post("/{service_name}/favorites/{fav_type}/{item_id}")
async def add_favorite(service_name: str, fav_type: str, item_id: str):
    service = _get_service(service_name)
    if not hasattr(service, "add_favorite"):
        return {"ok": False}
    ok = await service.add_favorite(fav_type, item_id)
    return {"ok": ok}


@router.delete("/{service_name}/favorites/{fav_type}/{item_id}")
async def remove_favorite(service_name: str, fav_type: str, item_id: str):
    service = _get_service(service_name)
    if not hasattr(service, "remove_favorite"):
        return {"ok": False}
    ok = await service.remove_favorite(fav_type, item_id)
    return {"ok": ok}


@router.get("/spotify/callback")
async def spotify_callback(code: str):
    """OAuth PKCE callback for Spotify. Spotify redirects here with ?code=..."""
    service = deps.streaming_services.get("spotify")
    if not service:
        raise HTTPException(status_code=503, detail="Spotify not configured")
    from tune_server.streaming.spotify import SpotifyService
    if isinstance(service, SpotifyService):
        await service.complete_auth(code, deps.db)
    return RedirectResponse("/")


    # Deezer callback removed — now uses ARL cookie auth via POST /streaming/deezer/auth
