-- Tune Server Database Schema

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT,
    musicbrainz_id TEXT UNIQUE,
    discogs_id TEXT,
    bio TEXT,
    image_path TEXT,
    image_source TEXT,
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
    original_year INTEGER,
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
    disc_subtitle TEXT,
    track_number INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    file_path TEXT UNIQUE,
    format TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER DEFAULT 2,
    file_mtime REAL,
    file_size INTEGER,
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
    stereo_pair_id TEXT,
    stereo_channel TEXT,
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
    avatar_url TEXT,
    pin_hash TEXT,
    is_admin INTEGER DEFAULT 0,
    eq_settings TEXT,
    quality_preference TEXT,
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

-- Track credits (multiple artists per track with roles/instruments)
CREATE TABLE IF NOT EXISTS track_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    artist_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'performer',
    instrument TEXT,
    position INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_track_credits_track ON track_credits(track_id);
CREATE INDEX IF NOT EXISTS idx_track_credits_artist ON track_credits(artist_id);

-- Performance indexes (v0.6.1)
CREATE INDEX IF NOT EXISTS idx_albums_created_at ON albums(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_albums_genre ON albums(genre);
CREATE INDEX IF NOT EXISTS idx_tracks_created_at ON tracks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_format_sr ON tracks(format, sample_rate);
CREATE INDEX IF NOT EXISTS idx_tracks_audio_hash ON tracks(audio_hash);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track ON playlist_tracks(track_id);
CREATE INDEX IF NOT EXISTS idx_albums_original_year ON albums(original_year);
CREATE INDEX IF NOT EXISTS idx_tracks_disc_number ON tracks(disc_number, track_number);

-- Metadata manager
CREATE TABLE IF NOT EXISTS metadata_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    current_value TEXT,
    suggested_value TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_metadata_suggestions_status ON metadata_suggestions(status);

CREATE TABLE IF NOT EXISTS metadata_fix_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    tracks_scanned INTEGER DEFAULT 0,
    auto_fixed INTEGER DEFAULT 0,
    suggestions INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    details TEXT
);

CREATE TABLE IF NOT EXISTS duplicate_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id_a INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    track_id_b INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    audio_hash TEXT NOT NULL,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS party_votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL,
    track_title TEXT NOT NULL,
    track_artist TEXT,
    queue_position INTEGER NOT NULL,
    vote_count INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_party_votes_zone ON party_votes(zone_id);

-- Album ratings & notes
CREATE TABLE IF NOT EXISTS album_ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    profile_id INTEGER,
    rating INTEGER CHECK(rating BETWEEN 1 AND 5),
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(album_id, profile_id)
);

-- Collections (album grouping)
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT DEFAULT 'folder',
    color TEXT DEFAULT '#6366f1',
    profile_id INTEGER,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collection_albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    album_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(collection_id, album_id)
);

-- Collaborative playlists
CREATE TABLE IF NOT EXISTS collaborative_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_by INTEGER,
    is_public BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collaborative_playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES collaborative_playlists(id) ON DELETE CASCADE,
    track_id INTEGER,
    track_title TEXT NOT NULL,
    track_artist TEXT,
    added_by INTEGER,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    votes INTEGER DEFAULT 0
);

-- Zone audio profiles (room correction / per-zone EQ)
CREATE TABLE IF NOT EXISTS zone_audio_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT 'Default',
    eq_preset TEXT,
    bass_boost REAL DEFAULT 0,
    treble_boost REAL DEFAULT 0,
    loudness_compensation BOOLEAN DEFAULT 0,
    crossfeed TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, name)
);

-- v0.8.0: Smart Collections — auto-rule-based album collections.
-- Rules stored as JSON text. Membership is computed lazily, never
-- materialised — see tune_server.library.smart_collection.
CREATE TABLE IF NOT EXISTS smart_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT DEFAULT 'folder',
    color TEXT DEFAULT '#6366f1',
    rules TEXT NOT NULL,
    match_mode TEXT DEFAULT 'all',
    sort_by TEXT DEFAULT 'added_at',
    sort_order TEXT DEFAULT 'desc',
    max_albums INTEGER DEFAULT 500,
    auto_refresh INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync link snapshots (delta detection for bidirectional sync)
CREATE TABLE IF NOT EXISTS sync_link_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_link_id INTEGER NOT NULL REFERENCES playlist_links(id) ON DELETE CASCADE,
    side TEXT NOT NULL,  -- 'local' or 'remote'
    tracks_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_link_snapshots_link ON sync_link_snapshots(playlist_link_id, side);

CREATE TABLE IF NOT EXISTS smart_playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    rules TEXT NOT NULL,
    match_mode TEXT DEFAULT 'all',
    sort_by TEXT DEFAULT 'title',
    sort_order TEXT DEFAULT 'asc',
    max_tracks INTEGER DEFAULT 200,
    auto_refresh INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User-defined tags/labels for tracks, albums, and artists
CREATE TABLE IF NOT EXISTS user_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#6366f1',
    icon TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_tag_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id INTEGER NOT NULL REFERENCES user_tags(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,  -- 'track', 'album', 'artist'
    item_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tag_id, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_user_tag_items_tag ON user_tag_items(tag_id);
CREATE INDEX IF NOT EXISTS idx_user_tag_items_item ON user_tag_items(item_type, item_id);
