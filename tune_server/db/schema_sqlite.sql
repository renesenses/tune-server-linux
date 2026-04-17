-- Tune Server Database Schema

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    musicbrainz_id TEXT UNIQUE,
    discogs_id TEXT,
    bio TEXT,
    image_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_artists_sort_name ON artists(sort_name);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    year INTEGER,
    genre TEXT,
    disc_count INTEGER DEFAULT 1,
    track_count INTEGER DEFAULT 0,
    cover_path TEXT,
    source TEXT NOT NULL DEFAULT 'local',  -- local, tidal, qobuz
    source_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_artist_id ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_year ON albums(year);
CREATE INDEX IF NOT EXISTS idx_albums_source ON albums(source, source_id);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    album_id INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    disc_number INTEGER DEFAULT 1,
    track_number INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    file_path TEXT UNIQUE,
    format TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER DEFAULT 2,
    file_mtime REAL,
    audio_hash TEXT,
    source TEXT NOT NULL DEFAULT 'local',
    source_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_id ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source, source_id);

CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks(playlist_id, position);

CREATE TABLE IF NOT EXISTS output_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    ip_address TEXT,
    port INTEGER,
    mac_address TEXT,
    icon TEXT,
    capabilities TEXT,
    firmware_version TEXT,
    is_available INTEGER DEFAULT 1,
    is_hidden INTEGER DEFAULT 0,
    last_seen_at TIMESTAMP,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_output_devices_uid ON output_devices(uid);
CREATE INDEX IF NOT EXISTS idx_output_devices_type ON output_devices(type);
CREATE INDEX IF NOT EXISTS idx_output_devices_available ON output_devices(is_available);

CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    output_type TEXT NOT NULL DEFAULT 'local',
    output_device_id TEXT,
    volume REAL DEFAULT 0.5,
    group_id TEXT,
    sync_delay_ms INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS play_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    is_current INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_play_queue_zone ON play_queue(zone_id, position);

CREATE TABLE IF NOT EXISTS streaming_auth (
    service TEXT PRIMARY KEY,
    token_data TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Full-Text Search virtual tables
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title,
    content='tracks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS albums_fts USING fts5(
    title,
    content='albums',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS artists_fts USING fts5(
    name,
    content='artists',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, title) VALUES (new.id, new.title);
END;
CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title) VALUES ('delete', old.id, old.title);
END;
CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, title) VALUES ('delete', old.id, old.title);
    INSERT INTO tracks_fts(rowid, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER IF NOT EXISTS albums_ai AFTER INSERT ON albums BEGIN
    INSERT INTO albums_fts(rowid, title) VALUES (new.id, new.title);
END;
CREATE TRIGGER IF NOT EXISTS albums_ad AFTER DELETE ON albums BEGIN
    INSERT INTO albums_fts(albums_fts, rowid, title) VALUES ('delete', old.id, old.title);
END;
CREATE TRIGGER IF NOT EXISTS albums_au AFTER UPDATE ON albums BEGIN
    INSERT INTO albums_fts(albums_fts, rowid, title) VALUES ('delete', old.id, old.title);
    INSERT INTO albums_fts(rowid, title) VALUES (new.id, new.title);
END;

CREATE TRIGGER IF NOT EXISTS artists_ai AFTER INSERT ON artists BEGIN
    INSERT INTO artists_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS artists_ad AFTER DELETE ON artists BEGIN
    INSERT INTO artists_fts(artists_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS artists_au AFTER UPDATE ON artists BEGIN
    INSERT INTO artists_fts(artists_fts, rowid, name) VALUES ('delete', old.id, old.name);
    INSERT INTO artists_fts(rowid, name) VALUES (new.id, new.name);
END;

-- Radio favorites (tracks heard on radio, saved by the user)
CREATE TABLE IF NOT EXISTS radio_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    station_name TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    stream_url TEXT,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_radio_favorites_dedup
    ON radio_favorites(title, artist);

-- User profiles & favorites
CREATE TABLE IF NOT EXISTS user_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    avatar_color TEXT DEFAULT '#FF6B35',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    artist_id INTEGER REFERENCES artists(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_fav_track ON user_favorites(user_id, track_id) WHERE track_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_fav_album ON user_favorites(user_id, album_id) WHERE album_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_fav_artist ON user_favorites(user_id, artist_id) WHERE artist_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_user_favorites_user ON user_favorites(user_id);

-- Performance indexes (v0.6.1)
CREATE INDEX IF NOT EXISTS idx_albums_created_at ON albums(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_albums_genre ON albums(genre);
CREATE INDEX IF NOT EXISTS idx_tracks_created_at ON tracks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_format_sr ON tracks(format, sample_rate);
CREATE INDEX IF NOT EXISTS idx_tracks_audio_hash ON tracks(audio_hash);
CREATE INDEX IF NOT EXISTS idx_radio_stations_genre ON radio_stations(genre);
CREATE INDEX IF NOT EXISTS idx_radio_stations_favorite ON radio_stations(favorite);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks(track_id);
CREATE INDEX IF NOT EXISTS idx_metadata_suggestions_status ON metadata_suggestions(status);
