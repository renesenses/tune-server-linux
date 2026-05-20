use pyo3::prelude::*;
use std::sync::Arc;
use tokio::sync::Mutex;

use tune_core::http::streamer::{AudioStreamer, StreamInfo};

struct StreamerInner {
    streamer: Arc<AudioStreamer>,
    runtime: tokio::runtime::Runtime,
    server_ip: String,
}

#[pyclass]
pub struct RustAudioStreamer {
    inner: Arc<Mutex<StreamerInner>>,
}

#[pymethods]
impl RustAudioStreamer {
    #[new]
    #[pyo3(signature = (port=8080, server_ip="127.0.0.1"))]
    fn new(port: u16, server_ip: &str) -> PyResult<Self> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("tokio: {e}")))?;

        let streamer = Arc::new(AudioStreamer::new(port));

        Ok(Self {
            inner: Arc::new(Mutex::new(StreamerInner {
                streamer,
                runtime,
                server_ip: server_ip.to_string(),
            })),
        })
    }

    fn create_file_session(
        &self,
        py: Python<'_>,
        file_path: &str,
        format: &str,
        mime_type: &str,
        sample_rate: u32,
        bit_depth: u16,
        channels: u16,
        file_size: Option<u64>,
    ) -> PyResult<String> {
        let info = StreamInfo {
            format: format.to_string(),
            mime_type: mime_type.to_string(),
            sample_rate,
            bit_depth,
            channels,
            file_size,
        };
        let file_path = file_path.to_string();
        py.detach(|| {
            let inner = self.inner.blocking_lock();
            inner.runtime.block_on(
                inner.streamer.create_file_session(info, file_path, true),
            )
        }).pipe(Ok)
    }

    fn create_proxy_session(
        &self,
        py: Python<'_>,
        upstream_url: &str,
        format: &str,
        mime_type: &str,
        sample_rate: u32,
        bit_depth: u16,
        channels: u16,
        is_radio: bool,
    ) -> PyResult<String> {
        let info = StreamInfo {
            format: format.to_string(),
            mime_type: mime_type.to_string(),
            sample_rate,
            bit_depth,
            channels,
            file_size: None,
        };
        let url = upstream_url.to_string();
        py.detach(|| {
            let inner = self.inner.blocking_lock();
            inner.runtime.block_on(
                inner.streamer.create_proxy_session(info, url, is_radio),
            )
        }).pipe(Ok)
    }

    fn get_stream_url(&self, session_id: &str, ext: &str) -> String {
        let inner = self.inner.blocking_lock();
        inner.streamer.get_stream_url(session_id, &inner.server_ip, ext)
    }

    fn remove_session(&self, py: Python<'_>, session_id: &str) -> PyResult<()> {
        let sid = session_id.to_string();
        py.detach(|| {
            let inner = self.inner.blocking_lock();
            inner.runtime.block_on(inner.streamer.remove_session(&sid));
        });
        Ok(())
    }

    fn session_count(&self) -> usize {
        let inner = self.inner.blocking_lock();
        inner.runtime.block_on(async {
            inner.streamer.sessions_state().lock().await.len()
        })
    }
}

trait Pipe: Sized {
    fn pipe<F, R>(self, f: F) -> R
    where
        F: FnOnce(Self) -> R,
    {
        f(self)
    }
}

impl<T> Pipe for T {}

pub fn register(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    m.add_class::<RustAudioStreamer>()?;
    Ok(())
}
