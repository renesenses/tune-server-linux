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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

-- Radio favorites
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
