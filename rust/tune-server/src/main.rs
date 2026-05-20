use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::{delete, get, post};
use axum::Router;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use tower_http::cors::CorsLayer;
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

use tune_core::db::album_repo::AlbumRepo;
use tune_core::db::artist_repo::ArtistRepo;
use tune_core::db::history_repo::HistoryRepo;
use tune_core::db::models::{Album, Artist, Track};
use tune_core::db::play_queue_repo::PlayQueueRepo;
use tune_core::db::playlist_repo::PlaylistRepo;
use tune_core::db::sqlite::SqliteDb;
use tune_core::db::track_repo::TrackRepo;
use tune_core::db::zone_repo::ZoneRepo;
use tune_core::discovery::ssdp::{get_local_ip, SsdpScanner};
use tune_core::http::streamer::AudioStreamer;
use tune_core::orchestrator::{PlayRequest, PlaybackOrchestrator};
use tune_core::outputs::registry::OutputRegistry;
use tune_core::playback::PlaybackManager;
use tune_core::poller::PositionPoller;
use tune_core::streaming::registry::ServiceRegistry;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_API_PORT: u16 = 9888;
const DEFAULT_STREAM_PORT: u16 = 9080;

struct AppState {
    db: SqliteDb,
    orchestrator: Arc<PlaybackOrchestrator>,
    playback: Arc<PlaybackManager>,
    streamer: Arc<AudioStreamer>,
    ssdp: Arc<Mutex<SsdpScanner>>,
    outputs: Arc<Mutex<OutputRegistry>>,
    services: Arc<Mutex<ServiceRegistry>>,
    music_dirs: Vec<String>,
    web_dir: Option<PathBuf>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("TUNE_LOG")
                .unwrap_or_else(|_| "info,tune_core=debug,tune_server=debug".parse().unwrap()),
        )
        .init();

    info!(version = VERSION, "tune_server_starting");

    let music_dirs = std::env::var("TUNE_MUSIC_DIRS")
        .map(|s| {
            serde_json::from_str::<Vec<String>>(&s)
                .unwrap_or_else(|_| vec![s.trim_matches('"').to_string()])
        })
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("."));
            vec![home.join("Music").to_string_lossy().to_string()]
        });

    let db_path = std::env::var("TUNE_DB_PATH").unwrap_or_else(|_| "tune_v2.db".into());
    let api_port: u16 = std::env::var("TUNE_API_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(DEFAULT_API_PORT);
    let stream_port: u16 = std::env::var("TUNE_STREAM_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(DEFAULT_STREAM_PORT);

    let web_dir = std::env::var("TUNE_WEB_DIR")
        .ok()
        .map(PathBuf::from)
        .or_else(|| {
            let exe = std::env::current_exe().ok()?;
            let dir = exe.parent()?;
            let candidate = dir.join("web");
            if candidate.join("index.html").is_file() {
                Some(candidate)
            } else {
                None
            }
        });

    info!(db = %db_path, music_dirs = ?music_dirs, api_port, stream_port, "config");

    let db = SqliteDb::open(&db_path).expect("failed to open database");
    db.init_schema().expect("failed to initialize schema");
    info!("database_ready");

    let streamer = Arc::new(AudioStreamer::new(stream_port));
    let playback = Arc::new(PlaybackManager::new());
    let outputs = Arc::new(Mutex::new(OutputRegistry::new()));
    let services = Arc::new(Mutex::new(ServiceRegistry::new()));

    let orchestrator = Arc::new(PlaybackOrchestrator {
        db: db.clone(),
        playback: playback.clone(),
        streamer: streamer.clone(),
        services: services.clone(),
        outputs: outputs.clone(),
    });

    let (ssdp_tx, mut ssdp_rx) = tokio::sync::mpsc::channel(64);
    let ssdp = Arc::new(Mutex::new(SsdpScanner::new(ssdp_tx)));

    // Start SSDP discovery
    {
        let scanner = ssdp.clone();
        let outputs_ref = outputs.clone();
        tokio::spawn(async move {
            let mut s = scanner.lock().await;
            s.start().await;
            drop(s);
            info!("ssdp_discovery_started");

            while let Some(event) = ssdp_rx.recv().await {
                use tune_core::discovery::ssdp::SsdpEvent;
                match event {
                    SsdpEvent::DeviceDiscovered(dev) => {
                        info!(name = %dev.name, host = %dev.host, "device_discovered");
                        // Extract service URLs from capabilities
                        let service_urls: std::collections::HashMap<String, String> = dev
                            .capabilities
                            .get("service_urls")
                            .and_then(|v| serde_json::from_value(v.clone()).ok())
                            .unwrap_or_default();
                        if let Some(av_url) = service_urls.get("avtransport") {
                            let rc_url = service_urls.get("renderingcontrol")
                                .cloned()
                                .unwrap_or_default();
                            let base = format!("http://{}:{}", dev.host, dev.port);
                            let full_av = if av_url.starts_with("http") {
                                av_url.clone()
                            } else {
                                format!("{}{}", base, av_url)
                            };
                            let full_rc = if rc_url.starts_with("http") {
                                rc_url
                            } else {
                                format!("{}{}", base, rc_url)
                            };
                            let dlna = tune_core::outputs::dlna::DlnaOutput::new(
                                dev.name.clone(),
                                dev.id.clone(),
                                dev.host.clone(),
                                full_av,
                                full_rc,
                            );
                            outputs_ref.lock().await.register(Box::new(dlna));
                        }
                    }
                    SsdpEvent::DeviceLost(id) => {
                        info!(id = %id, "device_lost");
                        outputs_ref.lock().await.remove(&id);
                    }
                }
            }
        });
    }

    // Start position poller (gapless + auto-next)
    {
        let poller = PositionPoller::new(
            orchestrator.clone(),
            playback.clone(),
            outputs.clone(),
            db.clone(),
        );
        poller.spawn();
        info!("position_poller_started");
    }

    // Background library scan
    {
        let db_clone = db.clone();
        let dirs = music_dirs.clone();
        tokio::spawn(async move {
            info!("library_scan_starting");
            match tokio::task::spawn_blocking(move || {
                tune_core::scanner::scan_and_import(&db_clone, &dirs)
            })
            .await
            {
                Ok(Ok(stats)) => info!(
                    scanned = stats.total_files,
                    added = stats.metadata_ok,
                    "library_scan_complete"
                ),
                Ok(Err(e)) => warn!(error = %e, "library_scan_error"),
                Err(e) => warn!(error = %e, "library_scan_task_panic"),
            }
        });
    }

    let state = Arc::new(AppState {
        db: db.clone(),
        orchestrator,
        playback,
        streamer: streamer.clone(),
        ssdp,
        outputs,
        services,
        music_dirs,
        web_dir: web_dir.clone(),
    });

    // Build API router
    let api = Router::new()
        // System
        .route("/system/info", get(system_info))
        .route("/system/health", get(system_health))
        // Library
        .route("/library/albums", get(list_albums))
        .route("/library/albums/count", get(albums_count))
        .route("/library/albums/{album_id}", get(get_album))
        .route("/library/albums/{album_id}/tracks", get(get_album_tracks))
        .route("/library/artists", get(list_artists))
        .route("/library/artists/{artist_id}", get(get_artist))
        .route("/library/artists/{artist_id}/albums", get(get_artist_albums))
        .route("/library/tracks", get(list_tracks))
        .route("/library/tracks/{track_id}", get(get_track))
        .route("/library/search", get(search_library))
        .route("/library/stats", get(library_stats))
        .route("/library/scan", post(trigger_scan))
        .route("/library/browse/roots", get(browse_roots))
        .route("/library/browse/dir", get(browse_dir))
        // System config
        .route("/system/music-dirs", get(get_music_dirs))
        .route("/system/music-dirs", post(add_music_dir))
        .route("/system/music-dirs", delete(remove_music_dir))
        .route("/system/scan", post(trigger_scan))
        .route("/system/scan/status", get(scan_status))
        // Playback
        .route("/playback/play", post(playback_play))
        .route("/playback/pause", post(playback_pause))
        .route("/playback/resume", post(playback_resume))
        .route("/playback/stop", post(playback_stop))
        .route("/playback/seek", post(playback_seek))
        .route("/playback/volume", post(playback_volume))
        // Queue
        .route("/zones/{zone_id}/queue", get(get_queue))
        .route("/zones/{zone_id}/queue", post(set_queue))
        .route("/zones/{zone_id}/queue", delete(clear_queue))
        // Zones
        .route("/zones", get(list_zones))
        .route("/zones", post(create_zone))
        .route("/zones/{zone_id}", get(get_zone))
        .route("/zones/{zone_id}", delete(delete_zone))
        // Devices
        .route("/devices", get(list_devices))
        // Playlists
        .route("/playlists", get(list_playlists))
        .route("/playlists", post(create_playlist))
        .route("/playlists/{playlist_id}", get(get_playlist))
        .route("/playlists/{playlist_id}", delete(delete_playlist))
        .route("/playlists/{playlist_id}/tracks", get(get_playlist_tracks))
        // History
        .route("/history/recent", get(recent_history))
        // Artwork
        .route("/library/artwork/{filename}", get(serve_artwork))
        // WebSocket
        .route("/ws", get(ws_handler));

    let stream_sessions = streamer.sessions_state();

    let mut app = Router::new()
        .nest("/api/v1", api)
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state.clone())
        .merge(tune_core::http::streamer::router(stream_sessions.clone()));
    // Note: merge works here because both sides are Router<()> after with_state

    // Serve web client static files
    if let Some(ref dir) = web_dir {
        if dir.join("index.html").is_file() {
            info!(path = %dir.display(), "serving_web_client");
            app = app.fallback_service(
                tower_http::services::ServeDir::new(dir)
                    .fallback(tower_http::services::ServeFile::new(dir.join("index.html"))),
            );
        }
    }

    // Start HTTP streaming server
    let stream_addr: SocketAddr = ([0, 0, 0, 0], stream_port).into();
    let stream_router = tune_core::http::streamer::router(stream_sessions).layer(CorsLayer::permissive());
    tokio::spawn(async move {
        let listener = tokio::net::TcpListener::bind(stream_addr).await.unwrap();
        info!(port = stream_port, "stream_server_listening");
        axum::serve(listener, stream_router).await.unwrap();
    });

    // Start API server
    let addr: SocketAddr = ([0, 0, 0, 0], api_port).into();
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();

    let local_ip = get_local_ip()
        .map(|ip| ip.to_string())
        .unwrap_or_else(|| "localhost".into());

    info!(
        port = api_port,
        url = format!("http://{}:{}", local_ip, api_port),
        "tune_server_ready"
    );

    axum::serve(listener, app).await.unwrap();
}

// ---------------------------------------------------------------------------
// System routes
// ---------------------------------------------------------------------------

async fn system_info(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let local_ip = get_local_ip()
        .map(|ip| ip.to_string())
        .unwrap_or_default();
    Json(serde_json::json!({
        "version": VERSION,
        "engine": "rust",
        "local_ip": local_ip,
        "music_dirs": s.music_dirs,
        "web_dir": s.web_dir.as_ref().map(|p| p.display().to_string()),
    }))
}

async fn system_health(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let track_count = TrackRepo::new(s.db.clone()).count().unwrap_or(0);
    let album_count = AlbumRepo::new(s.db.clone()).count().unwrap_or(0);
    let devices = s.ssdp.lock().await.devices().await;
    let zones = ZoneRepo::new(s.db.clone()).list().unwrap_or_default();
    Json(serde_json::json!({
        "status": "ok",
        "version": VERSION,
        "tracks": track_count,
        "albums": album_count,
        "devices": devices.len(),
        "zones": zones.len(),
    }))
}

// ---------------------------------------------------------------------------
// Library routes
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct PaginationParams {
    limit: Option<i64>,
    offset: Option<i64>,
    #[serde(rename = "q")]
    query: Option<String>,
}

#[derive(Deserialize)]
struct AlbumListParams {
    limit: Option<i64>,
    offset: Option<i64>,
    sort: Option<String>,
    order: Option<String>,
    quality: Option<String>,
    format: Option<String>,
}

async fn list_albums(
    State(s): State<Arc<AppState>>,
    Query(p): Query<AlbumListParams>,
) -> Json<Vec<serde_json::Value>> {
    let repo = AlbumRepo::new(s.db.clone());
    let albums = repo
        .list_sorted(
            p.limit.unwrap_or(100),
            p.offset.unwrap_or(0),
            p.sort.as_deref().unwrap_or("title"),
            p.order.as_deref().unwrap_or("asc"),
            p.quality.as_deref(),
            p.format.as_deref(),
        )
        .unwrap_or_default();
    Json(albums.into_iter().map(|a| a.to_json()).collect())
}

async fn albums_count(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let count = AlbumRepo::new(s.db.clone()).count().unwrap_or(0);
    Json(serde_json::json!({ "count": count }))
}

async fn get_album(
    State(s): State<Arc<AppState>>,
    Path(album_id): Path<i64>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    AlbumRepo::new(s.db.clone())
        .get(album_id)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        .map(|a| Json(a.to_json()))
        .ok_or(StatusCode::NOT_FOUND)
}

async fn get_album_tracks(
    State(s): State<Arc<AppState>>,
    Path(album_id): Path<i64>,
) -> Json<Vec<serde_json::Value>> {
    let repo = TrackRepo::new(s.db.clone());
    let tracks = repo.list_by_album(album_id).unwrap_or_default();
    Json(tracks.into_iter().map(|t| track_to_json(&t)).collect())
}

async fn list_artists(
    State(s): State<Arc<AppState>>,
    Query(p): Query<PaginationParams>,
) -> Json<Vec<serde_json::Value>> {
    let repo = ArtistRepo::new(s.db.clone());
    let artists = repo
        .list(p.limit.unwrap_or(100), p.offset.unwrap_or(0))
        .unwrap_or_default();
    Json(artists.into_iter().map(|a| artist_to_json(&a)).collect())
}

async fn get_artist(
    State(s): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    ArtistRepo::new(s.db.clone())
        .get(artist_id)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        .map(|a| Json(artist_to_json(&a)))
        .ok_or(StatusCode::NOT_FOUND)
}

async fn get_artist_albums(
    State(s): State<Arc<AppState>>,
    Path(artist_id): Path<i64>,
) -> Json<Vec<serde_json::Value>> {
    let repo = AlbumRepo::new(s.db.clone());
    let albums = repo.list_by_artist(artist_id).unwrap_or_default();
    Json(albums.into_iter().map(|a| a.to_json()).collect())
}

async fn list_tracks(
    State(s): State<Arc<AppState>>,
    Query(p): Query<PaginationParams>,
) -> Json<Vec<serde_json::Value>> {
    let repo = TrackRepo::new(s.db.clone());
    let tracks = repo
        .list(p.limit.unwrap_or(100), p.offset.unwrap_or(0))
        .unwrap_or_default();
    Json(tracks.into_iter().map(|t| track_to_json(&t)).collect())
}

async fn get_track(
    State(s): State<Arc<AppState>>,
    Path(track_id): Path<i64>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    TrackRepo::new(s.db.clone())
        .get(track_id)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        .map(|t| Json(track_to_json(&t)))
        .ok_or(StatusCode::NOT_FOUND)
}

async fn search_library(
    State(s): State<Arc<AppState>>,
    Query(p): Query<PaginationParams>,
) -> Json<serde_json::Value> {
    let q = p.query.unwrap_or_default();
    if q.is_empty() {
        return Json(serde_json::json!({ "tracks": [], "albums": [], "artists": [] }));
    }
    let limit = p.limit.unwrap_or(20);
    let tracks = TrackRepo::new(s.db.clone())
        .search(&q, limit)
        .unwrap_or_default();
    let albums = AlbumRepo::new(s.db.clone())
        .search(&q, limit)
        .unwrap_or_default();
    let artists = ArtistRepo::new(s.db.clone())
        .search(&q, limit)
        .unwrap_or_default();
    Json(serde_json::json!({
        "tracks": tracks.into_iter().map(|t| track_to_json(&t)).collect::<Vec<_>>(),
        "albums": albums.into_iter().map(|a| a.to_json()).collect::<Vec<_>>(),
        "artists": artists.into_iter().map(|a| artist_to_json(&a)).collect::<Vec<_>>(),
    }))
}

async fn library_stats(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let tracks = TrackRepo::new(s.db.clone()).count().unwrap_or(0);
    let albums = AlbumRepo::new(s.db.clone()).count().unwrap_or(0);
    let artists = ArtistRepo::new(s.db.clone()).count().unwrap_or(0);
    Json(serde_json::json!({
        "tracks": tracks,
        "albums": albums,
        "artists": artists,
    }))
}

async fn trigger_scan(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let db = s.db.clone();
    let dirs = s.music_dirs.clone();
    tokio::spawn(async move {
        let _ = tokio::task::spawn_blocking(move || {
            tune_core::scanner::scan_and_import(&db, &dirs)
        })
        .await;
    });
    Json(serde_json::json!({ "status": "scan_started" }))
}

// ---------------------------------------------------------------------------
// Playback routes
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct PlayBody {
    zone_id: i64,
    track_id: Option<i64>,
    track_ids: Option<Vec<i64>>,
    position: Option<usize>,
    output_device_id: Option<String>,
    source: Option<String>,
    source_id: Option<String>,
}

async fn playback_play(
    State(s): State<Arc<AppState>>,
    Json(body): Json<PlayBody>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    // If track_ids provided, set up the queue and play from position
    if let Some(ref ids) = body.track_ids {
        if !ids.is_empty() {
            let queue_repo = PlayQueueRepo::new(s.db.clone());
            let _ = queue_repo.set_queue(body.zone_id, ids);
            let pos = body.position.unwrap_or(0);
            let play_id = ids.get(pos).or_else(|| ids.first()).copied();
            if let Some(tid) = play_id {
                let _ = queue_repo.set_current(body.zone_id, tid);

                let zone = ZoneRepo::new(s.db.clone()).get(body.zone_id).ok().flatten();
                let device_id = zone.and_then(|z| z.output_device_id);

                let req = PlayRequest {
                    zone_id: body.zone_id,
                    output_device_id: device_id,
                    track_id: Some(tid),
                    source: Some("local".into()),
                    source_id: None,
                    title: None,
                    artist_name: None,
                    album_title: None,
                    cover_url: None,
                    duration_ms: None,
                };
                match s.orchestrator.play(req).await {
                    Ok(result) => return Ok(Json(serde_json::json!({
                        "stream_url": result.stream_url,
                        "output_sent": result.output_sent,
                        "source": result.source,
                        "queue_length": ids.len(),
                    }))),
                    Err(e) => {
                        warn!(error = %e, "playback_play_queue_error");
                        return Err(StatusCode::INTERNAL_SERVER_ERROR);
                    }
                }
            }
        }
    }

    // Single track play
    let zone = ZoneRepo::new(s.db.clone()).get(body.zone_id).ok().flatten();
    let device_id = body.output_device_id.or_else(|| zone.and_then(|z| z.output_device_id));

    let req = PlayRequest {
        zone_id: body.zone_id,
        output_device_id: device_id,
        track_id: body.track_id,
        source: body.source,
        source_id: body.source_id,
        title: None,
        artist_name: None,
        album_title: None,
        cover_url: None,
        duration_ms: None,
    };
    match s.orchestrator.play(req).await {
        Ok(result) => Ok(Json(serde_json::json!({
            "stream_url": result.stream_url,
            "output_sent": result.output_sent,
            "source": result.source,
        }))),
        Err(e) => {
            warn!(error = %e, "playback_play_error");
            Err(StatusCode::INTERNAL_SERVER_ERROR)
        }
    }
}

#[derive(Deserialize)]
struct ZoneBody {
    zone_id: i64,
}

async fn playback_pause(
    State(s): State<Arc<AppState>>,
    Json(body): Json<ZoneBody>,
) -> StatusCode {
    s.orchestrator.pause(body.zone_id, None).await;
    StatusCode::OK
}

async fn playback_resume(
    State(s): State<Arc<AppState>>,
    Json(body): Json<ZoneBody>,
) -> StatusCode {
    s.orchestrator.resume(body.zone_id, None).await;
    StatusCode::OK
}

async fn playback_stop(
    State(s): State<Arc<AppState>>,
    Json(body): Json<ZoneBody>,
) -> StatusCode {
    s.orchestrator.stop(body.zone_id, None).await;
    StatusCode::OK
}

#[derive(Deserialize)]
struct SeekBody {
    zone_id: i64,
    position_ms: i64,
}

async fn playback_seek(
    State(s): State<Arc<AppState>>,
    Json(body): Json<SeekBody>,
) -> StatusCode {
    s.orchestrator.seek(body.zone_id, body.position_ms as u64, None).await;
    StatusCode::OK
}

#[derive(Deserialize)]
struct VolumeBody {
    zone_id: i64,
    volume: f64,
}

async fn playback_volume(
    State(s): State<Arc<AppState>>,
    Json(body): Json<VolumeBody>,
) -> StatusCode {
    s.orchestrator.set_volume(body.zone_id, body.volume, None).await;
    StatusCode::OK
}

// ---------------------------------------------------------------------------
// Zone routes
// ---------------------------------------------------------------------------

async fn list_zones(State(s): State<Arc<AppState>>) -> Json<Vec<serde_json::Value>> {
    let zones = ZoneRepo::new(s.db.clone()).list().unwrap_or_default();
    let mut result = Vec::new();
    for z in zones {
        let zone_id = z.id.unwrap_or(0);
        let state = s.playback.get_state(zone_id).await;
        result.push(serde_json::json!({
            "id": zone_id,
            "name": z.name,
            "output_type": z.output_type,
            "output_device_id": z.output_device_id,
            "volume": state.volume,
            "muted": state.muted,
            "state": format!("{:?}", state.state).to_lowercase(),
            "now_playing": state.now_playing.map(|np| serde_json::json!({
                "track_id": np.track_id,
                "title": np.title,
                "artist_name": np.artist_name,
                "album_title": np.album_title,
                "cover_path": np.cover_path,
                "duration_ms": np.duration_ms,
            })),
        }));
    }
    Json(result)
}

#[derive(Deserialize)]
struct CreateZoneBody {
    name: String,
    output_type: Option<String>,
    output_device_id: Option<String>,
}

async fn create_zone(
    State(s): State<Arc<AppState>>,
    Json(body): Json<CreateZoneBody>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    let repo = ZoneRepo::new(s.db.clone());
    let id = repo
        .create(&body.name, body.output_type.as_deref(), body.output_device_id.as_deref())
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(Json(serde_json::json!({ "id": id, "name": body.name })))
}

async fn get_zone(
    State(s): State<Arc<AppState>>,
    Path(zone_id): Path<i64>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    let zone = ZoneRepo::new(s.db.clone())
        .get(zone_id)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        .ok_or(StatusCode::NOT_FOUND)?;
    let state = s.playback.get_state(zone_id).await;
    Ok(Json(serde_json::json!({
        "id": zone_id,
        "name": zone.name,
        "output_type": zone.output_type,
        "output_device_id": zone.output_device_id,
        "state": format!("{:?}", state.state).to_lowercase(),
        "volume": state.volume,
    })))
}

async fn delete_zone(
    State(s): State<Arc<AppState>>,
    Path(zone_id): Path<i64>,
) -> StatusCode {
    ZoneRepo::new(s.db.clone())
        .delete(zone_id)
        .map(|_| StatusCode::NO_CONTENT)
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

// ---------------------------------------------------------------------------
// Queue routes
// ---------------------------------------------------------------------------

async fn get_queue(
    State(s): State<Arc<AppState>>,
    Path(zone_id): Path<i64>,
) -> Json<serde_json::Value> {
    let repo = PlayQueueRepo::new(s.db.clone());
    let items = repo.get_queue(zone_id).unwrap_or_default();
    let current = repo.get_current(zone_id).ok().flatten();
    Json(serde_json::json!({
        "items": items,
        "current_track_id": current.map(|c| c.track_id),
    }))
}

async fn set_queue(
    State(s): State<Arc<AppState>>,
    Path(zone_id): Path<i64>,
    Json(body): Json<serde_json::Value>,
) -> StatusCode {
    let track_ids: Vec<i64> = body
        .get("track_ids")
        .and_then(|v| serde_json::from_value(v.clone()).ok())
        .unwrap_or_default();
    PlayQueueRepo::new(s.db.clone())
        .set_queue(zone_id, &track_ids)
        .map(|_| StatusCode::OK)
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

async fn clear_queue(
    State(s): State<Arc<AppState>>,
    Path(zone_id): Path<i64>,
) -> StatusCode {
    PlayQueueRepo::new(s.db.clone())
        .clear(zone_id)
        .map(|_| StatusCode::NO_CONTENT)
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

// ---------------------------------------------------------------------------
// Device routes
// ---------------------------------------------------------------------------

async fn list_devices(State(s): State<Arc<AppState>>) -> Json<Vec<serde_json::Value>> {
    let devices = s.ssdp.lock().await.devices().await;
    Json(
        devices
            .iter()
            .map(|d| {
                serde_json::json!({
                    "id": d.id,
                    "name": d.name,
                    "type": format!("{}", d.device_type),
                    "host": d.host,
                    "port": d.port,
                    "manufacturer": d.manufacturer,
                    "model": d.model,
                })
            })
            .collect(),
    )
}

// ---------------------------------------------------------------------------
// Playlist routes
// ---------------------------------------------------------------------------

async fn list_playlists(State(s): State<Arc<AppState>>) -> Json<Vec<serde_json::Value>> {
    let playlists = PlaylistRepo::new(s.db.clone()).list(1000, 0).unwrap_or_default();
    Json(
        playlists
            .into_iter()
            .map(|p| {
                serde_json::json!({
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "track_count": p.track_count,
                })
            })
            .collect(),
    )
}

#[derive(Deserialize)]
struct CreatePlaylistBody {
    name: String,
    description: Option<String>,
}

async fn create_playlist(
    State(s): State<Arc<AppState>>,
    Json(body): Json<CreatePlaylistBody>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    let id = PlaylistRepo::new(s.db.clone())
        .create(&body.name, body.description.as_deref())
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(Json(serde_json::json!({ "id": id, "name": body.name })))
}

async fn get_playlist(
    State(s): State<Arc<AppState>>,
    Path(playlist_id): Path<i64>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    PlaylistRepo::new(s.db.clone())
        .get(playlist_id)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        .map(|p| {
            Json(serde_json::json!({
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "track_count": p.track_count,
            }))
        })
        .ok_or(StatusCode::NOT_FOUND)
}

async fn delete_playlist(
    State(s): State<Arc<AppState>>,
    Path(playlist_id): Path<i64>,
) -> StatusCode {
    PlaylistRepo::new(s.db.clone())
        .delete(playlist_id)
        .map(|_| StatusCode::NO_CONTENT)
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

async fn get_playlist_tracks(
    State(s): State<Arc<AppState>>,
    Path(playlist_id): Path<i64>,
) -> Json<Vec<serde_json::Value>> {
    let track_ids = PlaylistRepo::new(s.db.clone())
        .get_track_ids(playlist_id)
        .unwrap_or_default();
    let tracks = TrackRepo::new(s.db.clone())
        .get_multiple(&track_ids)
        .unwrap_or_default();
    Json(tracks.into_iter().map(|t| track_to_json(&t)).collect())
}

// ---------------------------------------------------------------------------
// History routes
// ---------------------------------------------------------------------------

async fn recent_history(
    State(s): State<Arc<AppState>>,
    Query(p): Query<PaginationParams>,
) -> Json<Vec<serde_json::Value>> {
    let records = HistoryRepo::new(s.db.clone())
        .recent(p.limit.unwrap_or(50))
        .unwrap_or_default();
    Json(
        records
            .into_iter()
            .map(|r| {
                serde_json::json!({
                    "id": r.id,
                    "track_id": r.track_id,
                    "title": r.title,
                    "artist_name": r.artist_name,
                    "album_title": r.album_title,
                    "listened_at": r.listened_at,
                })
            })
            .collect(),
    )
}

// ---------------------------------------------------------------------------
// Artwork routes
// ---------------------------------------------------------------------------

async fn serve_artwork(Path(filename): Path<String>) -> Result<axum::response::Response, StatusCode> {
    use axum::body::Body;
    use axum::http::header;

    let cache_dir = std::env::var("TUNE_ARTWORK_CACHE")
        .unwrap_or_else(|_| "artwork_cache".into());
    let path = std::path::Path::new(&cache_dir).join(&filename);

    if !path.is_file() {
        return Err(StatusCode::NOT_FOUND);
    }

    let data = tokio::fs::read(&path)
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    let content_type = if filename.ends_with(".png") {
        "image/png"
    } else {
        "image/jpeg"
    };

    Ok(axum::response::Response::builder()
        .header(header::CONTENT_TYPE, content_type)
        .header(header::CACHE_CONTROL, "public, max-age=86400")
        .body(Body::from(data))
        .unwrap())
}

// ---------------------------------------------------------------------------
// WebSocket handler
// ---------------------------------------------------------------------------

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> axum::response::Response {
    ws.on_upgrade(move |socket| handle_ws_connection(socket, state))
}

async fn handle_ws_connection(socket: WebSocket, state: Arc<AppState>) {
    let (mut sender, mut receiver) = socket.split();
    let mut event_rx = state.playback.subscribe();

    info!("ws_client_connected");

    loop {
        tokio::select! {
            // Forward playback events to the WebSocket client
            event = event_rx.recv() => {
                match event {
                    Ok(ev) => {
                        let mut data = ev.data.clone();
                        if let Some(obj) = data.as_object_mut() {
                            obj.insert("zone_id".to_string(), serde_json::json!(ev.zone_id));
                        }
                        let msg = serde_json::json!({
                            "type": format!("playback.{}", ev.event),
                            "data": data,
                        });
                        if sender.send(Message::Text(msg.to_string().into())).await.is_err() {
                            break;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                        warn!(skipped = n, "ws_client_lagged");
                        continue;
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                        break;
                    }
                }
            }
            // Handle incoming messages from the client (or detect disconnect)
            msg = receiver.next() => {
                match msg {
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(Message::Ping(data))) => {
                        if sender.send(Message::Pong(data)).await.is_err() {
                            break;
                        }
                    }
                    Some(Ok(_)) => {} // Ignore other messages
                    Some(Err(_)) => break,
                }
            }
        }
    }

    info!("ws_client_disconnected");
}

// ---------------------------------------------------------------------------
// Music dirs / onboarding routes
// ---------------------------------------------------------------------------

async fn get_music_dirs(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    Json(serde_json::json!({ "music_dirs": s.music_dirs }))
}

async fn add_music_dir(
    State(s): State<Arc<AppState>>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("");
    let mut dirs = s.music_dirs.clone();
    if !path.is_empty() && !dirs.contains(&path.to_string()) {
        dirs.push(path.to_string());
    }
    Json(serde_json::json!({ "music_dirs": dirs }))
}

async fn remove_music_dir(
    State(s): State<Arc<AppState>>,
    Json(body): Json<serde_json::Value>,
) -> Json<serde_json::Value> {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("");
    let dirs: Vec<&String> = s.music_dirs.iter().filter(|d| d.as_str() != path).collect();
    Json(serde_json::json!({ "music_dirs": dirs }))
}

async fn browse_roots(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let roots: Vec<serde_json::Value> = s.music_dirs.iter().map(|d| {
        let p = std::path::Path::new(d);
        serde_json::json!({
            "path": d,
            "name": p.file_name().and_then(|n| n.to_str()).unwrap_or(d),
            "exists": p.is_dir(),
        })
    }).collect();
    Json(serde_json::json!({ "roots": roots }))
}

#[derive(Deserialize)]
struct BrowseParams {
    path: String,
}

async fn browse_dir(Query(p): Query<BrowseParams>) -> Json<serde_json::Value> {
    let dir = std::path::Path::new(&p.path);
    if !dir.is_dir() {
        return Json(serde_json::json!({ "path": p.path, "exists": false, "entries": [] }));
    }
    let mut entries = Vec::new();
    if let Ok(read) = std::fs::read_dir(dir) {
        for entry in read.flatten() {
            if let Ok(ft) = entry.file_type() {
                if ft.is_dir() {
                    let name = entry.file_name().to_string_lossy().to_string();
                    if !name.starts_with('.') {
                        entries.push(serde_json::json!({
                            "name": name,
                            "path": entry.path().to_string_lossy(),
                            "is_dir": true,
                        }));
                    }
                }
            }
        }
    }
    entries.sort_by(|a, b| {
        a.get("name").and_then(|v| v.as_str()).unwrap_or("")
            .cmp(b.get("name").and_then(|v| v.as_str()).unwrap_or(""))
    });
    Json(serde_json::json!({ "path": p.path, "exists": true, "entries": entries }))
}

async fn scan_status(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let tracks = TrackRepo::new(s.db.clone()).count().unwrap_or(0);
    Json(serde_json::json!({
        "scanning": false,
        "tracks": tracks,
    }))
}

// ---------------------------------------------------------------------------
// JSON helpers
// ---------------------------------------------------------------------------

fn track_to_json(t: &Track) -> serde_json::Value {
    serde_json::json!({
        "id": t.id,
        "title": t.title,
        "album_id": t.album_id,
        "album_title": t.album_title,
        "artist_id": t.artist_id,
        "artist_name": t.artist_name,
        "track_number": t.track_number,
        "disc_number": t.disc_number,
        "duration_ms": t.duration_ms,
        "format": t.format,
        "sample_rate": t.sample_rate,
        "bit_depth": t.bit_depth,
        "channels": t.channels,
        "genre": t.genre,
        "composer": t.composer,
        "year": t.year,
        "file_path": t.file_path,
        "source": t.source,
    })
}

fn artist_to_json(a: &Artist) -> serde_json::Value {
    serde_json::json!({
        "id": a.id,
        "name": a.name,
        "sort_name": a.sort_name,
        "bio": a.bio,
        "image_path": a.image_path,
        "musicbrainz_id": a.musicbrainz_id,
    })
}
