-- Tune Server Database Schema (PostgreSQL)

CREATE TABLE IF NOT EXISTS artists (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    sort_name TEXT,
    musicbrainz_id TEXT UNIQUE,
    discogs_id TEXT,
    bio TEXT,
    image_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_artists_sort_name ON artists(sort_name);

CREATE TABLE IF NOT EXISTS albums (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    artist_id INTEGER REFERENCES artists(id) ON DELETE SET NULL,
    year INTEGER,
    genre TEXT,
    disc_count INTEGER DEFAULT 1,
    track_count INTEGER DEFAULT 0,
    cover_path TEXT,
    source TEXT NOT NULL DEFAULT 'local',
    source_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    musicbrainz_release_id TEXT,
    label TEXT,
    catalog_number TEXT,
    barcode TEXT,
    format TEXT,
    sample_rate INTEGER,
    bit_depth INTEGER,
    artist_name TEXT
);

CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_artist_id ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_year ON albums(year);
CREATE INDEX IF NOT EXISTS idx_albums_source ON albums(source, source_id);

CREATE TABLE IF NOT EXISTS tracks (
    id SERIAL PRIMARY KEY,
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    isrc TEXT,
    genre TEXT,
    composer TEXT,
    year INTEGER,
    lyrics TEXT,
    comment TEXT,
    musicbrainz_recording_id TEXT,
    acoustid TEXT,
    bpm REAL,
    label TEXT,
    custom_tags TEXT,
    album_title TEXT,
    artist_name TEXT,
    cover_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_id ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source, source_id);

CREATE TABLE IF NOT EXISTS playlists (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id SERIAL PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist ON playlist_tracks(playlist_id, position);

CREATE TABLE IF NOT EXISTS zones (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    output_type TEXT NOT NULL DEFAULT 'local',
    output_device_id TEXT,
    volume REAL DEFAULT 0.5,
    group_id TEXT,
    sync_delay_ms INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    queue_json TEXT,
    muted BOOLEAN DEFAULT FALSE,
    online BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS play_queue (
    id SERIAL PRIMARY KEY,
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

-- Radio favorites (tracks heard on radio, saved by the user)
CREATE TABLE IF NOT EXISTS radio_favorites (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    station_name TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    stream_url TEXT,
    saved_at TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_radio_favorites_dedup
    ON radio_favorites(title, artist);

-- Radio stations
CREATE TABLE IF NOT EXISTS radio_stations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    stream_url TEXT NOT NULL,
    logo_url TEXT,
    genre TEXT,
    tags TEXT,
    codec TEXT,
    country TEXT,
    homepage_url TEXT,
    favorite BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Device credentials (DLNA / renderer auth tokens)
CREATE TABLE IF NOT EXISTS device_credentials (
    device_id TEXT PRIMARY KEY,
    device_name TEXT,
    credentials TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Duplicate tracks detection
CREATE TABLE IF NOT EXISTS duplicate_tracks (
    id SERIAL PRIMARY KEY,
    track_id_a INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    track_id_b INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    audio_hash TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved BOOLEAN DEFAULT FALSE
);

-- Metadata fix reports
CREATE TABLE IF NOT EXISTS metadata_fix_reports (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    tracks_scanned INTEGER DEFAULT 0,
    auto_fixed INTEGER DEFAULT 0,
    suggestions INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    details TEXT
);

-- Metadata suggestions
CREATE TABLE IF NOT EXISTS metadata_suggestions (
    id SERIAL PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    album_id INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    field TEXT,
    current_value TEXT,
    suggested_value TEXT,
    source TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Network mounts (SMB/NFS)
CREATE TABLE IF NOT EXISTS network_mounts (
    id SERIAL PRIMARY KEY,
    host TEXT NOT NULL,
    share_name TEXT NOT NULL,
    protocol TEXT NOT NULL DEFAULT 'smb',
    mount_path TEXT,
    username TEXT,
    password TEXT,
    auto_mount BOOLEAN DEFAULT FALSE,
    status TEXT DEFAULT 'unmounted',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Playlist links (cross-service sync)
CREATE TABLE IF NOT EXISTS playlist_links (
    id SERIAL PRIMARY KEY,
    local_playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    service TEXT NOT NULL,
    service_playlist_id TEXT NOT NULL,
    service_playlist_name TEXT,
    sync_direction TEXT DEFAULT 'pull',
    last_synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Playlist snapshots
CREATE TABLE IF NOT EXISTS playlist_snapshots (
    id SERIAL PRIMARY KEY,
    source_service TEXT NOT NULL,
    source_playlist_id TEXT NOT NULL,
    playlist_name TEXT,
    track_count INTEGER DEFAULT 0,
    snapshot_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sync schedules
CREATE TABLE IF NOT EXISTS sync_schedules (
    id SERIAL PRIMARY KEY,
    playlist_link_id INTEGER REFERENCES playlist_links(id) ON DELETE CASCADE,
    interval_minutes INTEGER DEFAULT 60,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Transfer history (playlist import/export)
CREATE TABLE IF NOT EXISTS transfer_history (
    id SERIAL PRIMARY KEY,
    operation TEXT NOT NULL,
    source_service TEXT,
    source_playlist_id TEXT,
    source_playlist_name TEXT,
    target_service TEXT,
    target_playlist_id TEXT,
    target_playlist_name TEXT,
    total_tracks INTEGER DEFAULT 0,
    matched INTEGER DEFAULT 0,
    approximate INTEGER DEFAULT 0,
    not_found INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    details TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Zone groups (multi-room)
CREATE TABLE IF NOT EXISTS zone_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    leader_zone_id INTEGER REFERENCES zones(id) ON DELETE SET NULL,
    master_volume REAL DEFAULT 0.5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Zone group members
CREATE TABLE IF NOT EXISTS zone_group_members (
    group_id INTEGER NOT NULL REFERENCES zone_groups(id) ON DELETE CASCADE,
    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    volume_offset REAL DEFAULT 0.0,
    muted BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (group_id, zone_id)
);

-- Zone profiles (saved zone configurations)
CREATE TABLE IF NOT EXISTS zone_profiles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    config TEXT,
    icon TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Full-Text Search using tsvector + GIN indexes

-- Add tsvector columns (idempotent via DO block)
DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN fts_vector tsvector;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN fts_vector tsvector;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE artists ADD COLUMN fts_vector tsvector;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- GIN indexes for fast search
CREATE INDEX IF NOT EXISTS idx_tracks_fts ON tracks USING GIN(fts_vector);
CREATE INDEX IF NOT EXISTS idx_albums_fts ON albums USING GIN(fts_vector);
CREATE INDEX IF NOT EXISTS idx_artists_fts ON artists USING GIN(fts_vector);

-- Populate existing rows
UPDATE tracks SET fts_vector = to_tsvector('simple', COALESCE(title, '')) WHERE fts_vector IS NULL;
UPDATE albums SET fts_vector = to_tsvector('simple', COALESCE(title, '')) WHERE fts_vector IS NULL;
UPDATE artists SET fts_vector = to_tsvector('simple', COALESCE(name, '')) WHERE fts_vector IS NULL;

-- Triggers to maintain tsvector on INSERT/UPDATE
CREATE OR REPLACE FUNCTION tracks_fts_trigger() RETURNS trigger AS $$
BEGIN
    NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.title, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION albums_fts_trigger() RETURNS trigger AS $$
BEGIN
    NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.title, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION artists_fts_trigger() RETURNS trigger AS $$
BEGIN
    NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.name, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tracks_fts ON tracks;
CREATE TRIGGER trg_tracks_fts BEFORE INSERT OR UPDATE OF title ON tracks
    FOR EACH ROW EXECUTE FUNCTION tracks_fts_trigger();

DROP TRIGGER IF EXISTS trg_albums_fts ON albums;
CREATE TRIGGER trg_albums_fts BEFORE INSERT OR UPDATE OF title ON albums
    FOR EACH ROW EXECUTE FUNCTION albums_fts_trigger();

DROP TRIGGER IF EXISTS trg_artists_fts ON artists;
CREATE TRIGGER trg_artists_fts BEFORE INSERT OR UPDATE OF name ON artists
    FOR EACH ROW EXECUTE FUNCTION artists_fts_trigger();

-- User profiles & favorites
CREATE TABLE IF NOT EXISTS user_profiles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_color TEXT DEFAULT '#FF6B35',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_favorites (
    id SERIAL PRIMARY KEY,
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

-- =============================================================================
-- Backward-compatible column additions for existing databases
-- (idempotent: safe to run on databases that already have these columns)
-- =============================================================================

-- artists: add source_id
DO $$ BEGIN
    ALTER TABLE artists ADD COLUMN source_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- albums: add metadata columns
DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN musicbrainz_release_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN label TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN catalog_number TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN barcode TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN format TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN sample_rate INTEGER;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN bit_depth INTEGER;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE albums ADD COLUMN artist_name TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- tracks: add extended metadata columns
DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN isrc TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN genre TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN composer TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN year INTEGER;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN lyrics TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN comment TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN musicbrainz_recording_id TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN acoustid TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN bpm REAL;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN label TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN custom_tags TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN album_title TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN artist_name TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE tracks ADD COLUMN cover_path TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- zones: add queue_json, muted, online
DO $$ BEGIN
    ALTER TABLE zones ADD COLUMN queue_json TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE zones ADD COLUMN muted BOOLEAN DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE zones ADD COLUMN online BOOLEAN DEFAULT TRUE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

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
