use std::net::SocketAddr;
use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::{delete, get, post};
use axum::Router;
use serde::{Deserialize, Serialize};
use tower_http::cors::CorsLayer;
use tracing::info;

use tune_core::http::streamer::{AudioStreamer, StreamInfo};

const VERSION: &str = env!("CARGO_PKG_VERSION");

type SharedStreamer = Arc<AudioStreamer>;

// ─── Session API models ─────────────────────────────────────────

#[derive(Deserialize)]
struct FileSessionRequest {
    format: String,
    mime_type: String,
    sample_rate: u32,
    bit_depth: u16,
    channels: u16,
    file_size: Option<u64>,
    file_path: String,
    bit_perfect: bool,
}

#[derive(Deserialize)]
struct ProxySessionRequest {
    format: String,
    mime_type: String,
    sample_rate: u32,
    bit_depth: u16,
    channels: u16,
    file_size: Option<u64>,
    upstream_url: String,
    is_radio: bool,
}

#[derive(Serialize)]
struct SessionResponse {
    session_id: String,
}

#[derive(Serialize)]
struct SessionInfo {
    id: String,
    format: String,
    mime_type: String,
    sample_rate: u32,
    bit_depth: u16,
    channels: u16,
    age_secs: u64,
}

// ─── Handlers ───────────────────────────────────────────────────

async fn create_file_session(
    State(streamer): State<SharedStreamer>,
    Json(req): Json<FileSessionRequest>,
) -> (StatusCode, Json<SessionResponse>) {
    let info = StreamInfo {
        format: req.format,
        mime_type: req.mime_type,
        sample_rate: req.sample_rate,
        bit_depth: req.bit_depth,
        channels: req.channels,
        file_size: req.file_size,
    };
    let id = streamer
        .create_file_session(info, req.file_path, req.bit_perfect)
        .await;
    info!(session_id = %id, "file_session_created_via_api");
    (StatusCode::CREATED, Json(SessionResponse { session_id: id }))
}

async fn create_proxy_session(
    State(streamer): State<SharedStreamer>,
    Json(req): Json<ProxySessionRequest>,
) -> (StatusCode, Json<SessionResponse>) {
    let info = StreamInfo {
        format: req.format,
        mime_type: req.mime_type,
        sample_rate: req.sample_rate,
        bit_depth: req.bit_depth,
        channels: req.channels,
        file_size: req.file_size,
    };
    let id = streamer
        .create_proxy_session(info, req.upstream_url, req.is_radio)
        .await;
    info!(session_id = %id, "proxy_session_created_via_api");
    (StatusCode::CREATED, Json(SessionResponse { session_id: id }))
}

async fn remove_session(
    State(streamer): State<SharedStreamer>,
    axum::extract::Path(session_id): axum::extract::Path<String>,
) -> StatusCode {
    streamer.remove_session(&session_id).await;
    StatusCode::NO_CONTENT
}

async fn list_sessions(
    State(streamer): State<SharedStreamer>,
) -> Json<Vec<SessionInfo>> {
    let state = streamer.sessions_state();
    let sessions = state.lock().await;
    let list: Vec<SessionInfo> = sessions
        .values()
        .map(|s| SessionInfo {
            id: s.id.clone(),
            format: s.info.format.clone(),
            mime_type: s.info.mime_type.clone(),
            sample_rate: s.info.sample_rate,
            bit_depth: s.info.bit_depth,
            channels: s.info.channels,
            age_secs: s.created_at.elapsed().as_secs(),
        })
        .collect();
    Json(list)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "engine": "rust-sidecar",
        "version": VERSION,
    }))
}

// ─── Main ───────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("TUNE_SIDECAR_LOG")
                .unwrap_or_else(|_| "info,tune_core=info".parse().unwrap()),
        )
        .init();

    let port: u16 = std::env::var("TUNE_STREAM_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8080);

    let streamer = Arc::new(AudioStreamer::new(port));
    let sessions = streamer.sessions_state();

    // Session management API (called by Python)
    let mgmt = Router::new()
        .route("/health", get(health))
        .route("/sessions", get(list_sessions))
        .route("/sessions/file", post(create_file_session))
        .route("/sessions/proxy", post(create_proxy_session))
        .route("/sessions/{id}", delete(remove_session))
        .with_state(streamer);

    // Stream serving (called by DLNA renderers)
    let stream_router = tune_core::http::streamer::router(sessions);

    let app = Router::new()
        .merge(mgmt)
        .merge(stream_router)
        .layer(CorsLayer::permissive());

    let addr: SocketAddr = ([0, 0, 0, 0], port).into();
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();

    info!(port, version = VERSION, "tune_sidecar_ready");

    axum::serve(listener, app).await.unwrap();
}
