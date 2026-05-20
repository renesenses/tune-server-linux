pub mod hasher;
pub mod quality;
pub mod walker;
pub mod watcher;

use crate::db::album_repo::AlbumRepo;
use crate::db::artist_repo::ArtistRepo;
use crate::db::sqlite::SqliteDb;
use crate::db::track_repo::TrackRepo;
use crate::db::models::Track;
use tracing::info;

pub use walker::ScanStats;

pub fn scan_and_import(db: &SqliteDb, music_dirs: &[String]) -> Result<ScanStats, String> {
    let (files, stats) = walker::scan_directories(music_dirs, true, None);

    let artist_repo = ArtistRepo::new(db.clone());
    let album_repo = AlbumRepo::new(db.clone());
    let track_repo = TrackRepo::new(db.clone());

    let mut imported = 0;
    for f in &files {
        let meta = match &f.metadata {
            Some(m) => m,
            None => continue,
        };

        if track_repo.get_by_path(&f.path).unwrap_or(None).is_some() {
            continue;
        }

        let artist_name = meta.artist.as_deref().unwrap_or("Unknown Artist");
        let album_title = meta.album.as_deref().unwrap_or("Unknown Album");

        let artist = artist_repo
            .get_or_create(artist_name, None, None)
            .map_err(|e| format!("artist create error: {e}"))?;

        let artist_id = artist.id.unwrap_or(0);

        let album = album_repo
            .get_or_create(album_title, artist_id, meta.year.map(|y| y as i32))
            .map_err(|e| format!("album create error: {e}"))?;

        // Extract composer from credits if available
        let composer = meta.credits.iter()
            .find(|c| c.role == "composer")
            .map(|c| c.name.clone());

        let track = Track {
            id: None,
            title: meta.title.clone().unwrap_or_else(|| "Unknown".into()),
            album_id: album.id,
            album_title: Some(album_title.to_string()),
            artist_id: Some(artist_id),
            artist_name: Some(artist_name.to_string()),
            disc_number: meta.disc_number.unwrap_or(1) as i32,
            disc_subtitle: None,
            track_number: meta.track_number.unwrap_or(0) as i32,
            duration_ms: meta.duration_ms.unwrap_or(0) as i64,
            file_path: Some(f.path.clone()),
            format: meta.format.clone(),
            sample_rate: meta.sample_rate.map(|v| v as i32),
            bit_depth: meta.bit_depth.map(|v| v as i32),
            channels: meta.channels.unwrap_or(2) as i32,
            file_mtime: Some(f.mtime as f64),
            file_size: Some(f.file_size as i64),
            audio_hash: f.audio_hash.clone(),
            source: "local".into(),
            source_id: None,
            isrc: meta.isrc.clone(),
            genre: meta.genre.clone(),
            composer,
            year: meta.year.map(|y| y as i32),
            bpm: meta.bpm,
            label: meta.label.clone(),
            musicbrainz_recording_id: meta.musicbrainz_recording_id.clone(),
        };

        if track_repo.create(&track).is_ok() {
            imported += 1;
        }
    }

    let _ = album_repo.delete_orphans();

    info!(scanned = stats.total_files, imported, "scan_and_import_complete");

    Ok(stats)
}
