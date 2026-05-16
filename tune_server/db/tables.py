"""
Single source of truth for the Tune Server database schema.

Uses SQLAlchemy Core Table objects. FTS columns (fts_vector) are excluded —
full-text search is handled separately as a plugin.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

# ---------------------------------------------------------------------------
# artists
# ---------------------------------------------------------------------------
artists = sa.Table(
    "artists",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("sort_name", sa.Text),
    sa.Column("musicbrainz_id", sa.Text, unique=True),
    sa.Column("discogs_id", sa.Text),
    sa.Column("bio", sa.Text),
    sa.Column("image_path", sa.Text),
    sa.Column("image_source", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("source_id", sa.Text),
)

sa.Index("idx_artists_name", artists.c.name)
sa.Index("idx_artists_sort_name", artists.c.sort_name)

# ---------------------------------------------------------------------------
# albums
# ---------------------------------------------------------------------------
albums = sa.Table(
    "albums",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column(
        "artist_id",
        sa.Integer,
        sa.ForeignKey("artists.id", ondelete="SET NULL"),
    ),
    sa.Column("year", sa.Integer),
    sa.Column("genre", sa.Text),
    sa.Column("disc_count", sa.Integer, server_default="1"),
    sa.Column("track_count", sa.Integer, server_default="0"),
    sa.Column("cover_path", sa.Text),
    sa.Column("source", sa.Text, nullable=False, server_default="local"),
    sa.Column("source_id", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("musicbrainz_release_id", sa.Text),
    sa.Column("musicbrainz_release_group_id", sa.Text),
    sa.Column("original_year", sa.Integer),
    sa.Column("release_date", sa.Text),
    sa.Column("original_date", sa.Text),
    sa.Column("label", sa.Text),
    sa.Column("catalog_number", sa.Text),
    sa.Column("barcode", sa.Text),
    sa.Column("format", sa.Text),
    sa.Column("sample_rate", sa.Integer),
    sa.Column("bit_depth", sa.Integer),
    sa.Column("artist_name", sa.Text),
    sa.Column("bio", sa.Text),
)

sa.Index("idx_albums_title", albums.c.title)
sa.Index("idx_albums_artist_id", albums.c.artist_id)
sa.Index("idx_albums_year", albums.c.year)
sa.Index("idx_albums_source", albums.c.source, albums.c.source_id)
sa.Index("idx_albums_created_at", albums.c.created_at)
sa.Index("idx_albums_genre", albums.c.genre)
sa.Index("idx_albums_original_year", albums.c.original_year)

# ---------------------------------------------------------------------------
# tracks
# ---------------------------------------------------------------------------
tracks = sa.Table(
    "tracks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column(
        "album_id",
        sa.Integer,
        sa.ForeignKey("albums.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "artist_id",
        sa.Integer,
        sa.ForeignKey("artists.id", ondelete="SET NULL"),
    ),
    sa.Column("disc_number", sa.Integer, server_default="1"),
    sa.Column("disc_subtitle", sa.Text),
    sa.Column("track_number", sa.Integer, server_default="0"),
    sa.Column("duration_ms", sa.Integer, server_default="0"),
    sa.Column("file_path", sa.Text, unique=True),
    sa.Column("format", sa.Text),
    sa.Column("sample_rate", sa.Integer),
    sa.Column("bit_depth", sa.Integer),
    sa.Column("channels", sa.Integer, server_default="2"),
    sa.Column("file_mtime", sa.Float(precision=53)),
    sa.Column("audio_hash", sa.Text),
    sa.Column("source", sa.Text, nullable=False, server_default="local"),
    sa.Column("source_id", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("isrc", sa.Text),
    sa.Column("genre", sa.Text),
    sa.Column("composer", sa.Text),
    sa.Column("year", sa.Integer),
    sa.Column("lyrics", sa.Text),
    sa.Column("synced_lyrics", sa.Text),
    sa.Column("comment", sa.Text),
    sa.Column("musicbrainz_recording_id", sa.Text),
    sa.Column("acoustid", sa.Text),
    sa.Column("bpm", sa.Float),
    sa.Column("label", sa.Text),
    sa.Column("custom_tags", sa.Text),
    sa.Column("waveform_data", sa.Text),
    sa.Column("waveform_generated_at", sa.DateTime),
    sa.Column("album_title", sa.Text),
    sa.Column("artist_name", sa.Text),
    sa.Column("cover_path", sa.Text),
    sa.Column("loudness_lufs", sa.Float),
)

sa.Index("idx_tracks_album_id", tracks.c.album_id)
sa.Index("idx_tracks_artist_id", tracks.c.artist_id)
sa.Index("idx_tracks_file_path", tracks.c.file_path)
sa.Index("idx_tracks_source", tracks.c.source, tracks.c.source_id)
sa.Index("idx_tracks_created_at", tracks.c.created_at)
sa.Index("idx_tracks_format_sr", tracks.c.format, tracks.c.sample_rate)
sa.Index("idx_tracks_audio_hash", tracks.c.audio_hash)
sa.Index("idx_tracks_disc_number", tracks.c.disc_number, tracks.c.track_number)

# ---------------------------------------------------------------------------
# track_credits
# ---------------------------------------------------------------------------
track_credits = sa.Table(
    "track_credits",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "track_id",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "artist_id",
        sa.Integer,
        sa.ForeignKey("artists.id", ondelete="SET NULL"),
    ),
    sa.Column("artist_name", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False, server_default="performer"),
    sa.Column("instrument", sa.Text),
    sa.Column("position", sa.Integer, server_default="0"),
)

sa.Index("idx_track_credits_track", track_credits.c.track_id)
sa.Index("idx_track_credits_artist", track_credits.c.artist_id)

# ---------------------------------------------------------------------------
# playlists
# ---------------------------------------------------------------------------
playlists = sa.Table(
    "playlists",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# playlist_tracks
# ---------------------------------------------------------------------------
playlist_tracks = sa.Table(
    "playlist_tracks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "playlist_id",
        sa.Integer,
        sa.ForeignKey("playlists.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "track_id",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
)

sa.Index(
    "idx_playlist_tracks_playlist",
    playlist_tracks.c.playlist_id,
    playlist_tracks.c.position,
)
sa.Index("idx_playlist_tracks_track", playlist_tracks.c.track_id)

# ---------------------------------------------------------------------------
# output_devices — persisted audio output devices (DLNA, AirPlay, USB, local)
# ---------------------------------------------------------------------------
output_devices = sa.Table(
    "output_devices",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("uid", sa.Text, nullable=False, unique=True),  # DLNA UUID, AirPlay ID, USB hw:x,y
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),  # dlna, airplay, local, usb
    sa.Column("manufacturer", sa.Text),
    sa.Column("model", sa.Text),
    sa.Column("ip_address", sa.Text),
    sa.Column("port", sa.Integer),
    sa.Column("mac_address", sa.Text),
    sa.Column("icon", sa.Text),  # speaker, tv, headphones, amplifier, dac, ...
    sa.Column("capabilities", sa.Text),  # JSON: {formats, max_sample_rate, dsd, channels, ...}
    sa.Column("firmware_version", sa.Text),
    sa.Column("is_available", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("is_hidden", sa.Boolean, server_default=sa.text("FALSE")),  # user can hide devices
    sa.Column("last_seen_at", sa.DateTime),
    sa.Column("first_seen_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_output_devices_uid", output_devices.c.uid)
sa.Index("idx_output_devices_type", output_devices.c.type)
sa.Index("idx_output_devices_available", output_devices.c.is_available)

# ---------------------------------------------------------------------------
# zones
# ---------------------------------------------------------------------------
zones = sa.Table(
    "zones",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("output_type", sa.Text, nullable=False, server_default="local"),
    sa.Column("output_device_id", sa.Text),
    sa.Column("volume", sa.Float, server_default="0.5"),
    sa.Column("group_id", sa.Text),
    sa.Column("sync_delay_ms", sa.Integer, server_default="0"),
    sa.Column("stereo_pair_id", sa.Text),
    sa.Column("stereo_channel", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("queue_json", sa.Text),
    sa.Column("muted", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("online", sa.Boolean, server_default=sa.text("TRUE")),
)

# ---------------------------------------------------------------------------
# play_queue
# ---------------------------------------------------------------------------
play_queue = sa.Table(
    "play_queue",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "zone_id",
        sa.Integer,
        sa.ForeignKey("zones.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "track_id",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("is_current", sa.Integer, server_default="0"),
)

sa.Index("idx_play_queue_zone", play_queue.c.zone_id, play_queue.c.position)

# ---------------------------------------------------------------------------
# streaming_auth  (TEXT PK, no auto-increment)
# ---------------------------------------------------------------------------
streaming_auth = sa.Table(
    "streaming_auth",
    metadata,
    sa.Column("service", sa.Text, primary_key=True, autoincrement=False),
    sa.Column("token_data", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# radio_favorites
# ---------------------------------------------------------------------------
radio_favorites = sa.Table(
    "radio_favorites",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("artist", sa.Text, nullable=False, server_default=""),
    sa.Column("station_name", sa.Text, nullable=False, server_default=""),
    sa.Column("cover_url", sa.Text),
    sa.Column("stream_url", sa.Text),
    sa.Column("saved_at", sa.Text, nullable=False, server_default=sa.func.now()),
)

sa.Index(
    "idx_radio_favorites_dedup",
    radio_favorites.c.title,
    radio_favorites.c.artist,
    unique=True,
)

# ---------------------------------------------------------------------------
# radio_stations
# ---------------------------------------------------------------------------
radio_stations = sa.Table(
    "radio_stations",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("stream_url", sa.Text, nullable=False),
    sa.Column("logo_url", sa.Text),
    sa.Column("genre", sa.Text),
    sa.Column("tags", sa.Text),
    sa.Column("codec", sa.Text),
    sa.Column("country", sa.Text),
    sa.Column("homepage_url", sa.Text),
    sa.Column("favorite", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_radio_stations_genre", radio_stations.c.genre)
sa.Index("idx_radio_stations_favorite", radio_stations.c.favorite)

# ---------------------------------------------------------------------------
# user_profiles
# ---------------------------------------------------------------------------
user_profiles = sa.Table(
    "user_profiles",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("avatar_color", sa.Text, server_default="#FF6B35"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# user_favorites
# ---------------------------------------------------------------------------
user_favorites = sa.Table(
    "user_favorites",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "user_id",
        sa.Integer,
        sa.ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "track_id",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "album_id",
        sa.Integer,
        sa.ForeignKey("albums.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "artist_id",
        sa.Integer,
        sa.ForeignKey("artists.id", ondelete="CASCADE"),
    ),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_user_favorites_user", user_favorites.c.user_id)
# Note: partial unique indexes (WHERE track_id IS NOT NULL, etc.) are not
# representable via sa.Index in a dialect-neutral way.  They should be created
# by the migration / DDL script directly.

# ---------------------------------------------------------------------------
# device_credentials  (TEXT PK, no auto-increment)
# ---------------------------------------------------------------------------
device_credentials = sa.Table(
    "device_credentials",
    metadata,
    sa.Column("device_id", sa.Text, primary_key=True, autoincrement=False),
    sa.Column("device_name", sa.Text),
    sa.Column("credentials", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# network_mounts
# ---------------------------------------------------------------------------
network_mounts = sa.Table(
    "network_mounts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("host", sa.Text, nullable=False),
    sa.Column("share_name", sa.Text, nullable=False),
    sa.Column("protocol", sa.Text, nullable=False, server_default="smb"),
    sa.Column("mount_path", sa.Text),
    sa.Column("username", sa.Text),
    sa.Column("password", sa.Text),
    sa.Column("auto_mount", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("status", sa.Text, server_default="unmounted"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# duplicate_tracks
# ---------------------------------------------------------------------------
duplicate_tracks = sa.Table(
    "duplicate_tracks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "track_id_a",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "track_id_b",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
    ),
    sa.Column("audio_hash", sa.Text),
    sa.Column("detected_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("resolved", sa.Boolean, server_default=sa.text("FALSE")),
)

# ---------------------------------------------------------------------------
# metadata_fix_reports
# ---------------------------------------------------------------------------
metadata_fix_reports = sa.Table(
    "metadata_fix_reports",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("started_at", sa.DateTime),
    sa.Column("completed_at", sa.DateTime),
    sa.Column("tracks_scanned", sa.Integer, server_default="0"),
    sa.Column("auto_fixed", sa.Integer, server_default="0"),
    sa.Column("suggestions", sa.Integer, server_default="0"),
    sa.Column("errors", sa.Integer, server_default="0"),
    sa.Column("details", sa.Text),
)

# ---------------------------------------------------------------------------
# metadata_suggestions
# ---------------------------------------------------------------------------
metadata_suggestions = sa.Table(
    "metadata_suggestions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "track_id",
        sa.Integer,
        sa.ForeignKey("tracks.id", ondelete="CASCADE"),
    ),
    sa.Column(
        "album_id",
        sa.Integer,
        sa.ForeignKey("albums.id", ondelete="CASCADE"),
    ),
    sa.Column("field", sa.Text),
    sa.Column("current_value", sa.Text),
    sa.Column("suggested_value", sa.Text),
    sa.Column("source", sa.Text),
    sa.Column("confidence", sa.Float),
    sa.Column("status", sa.Text, server_default="pending"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_metadata_suggestions_status", metadata_suggestions.c.status)

# ---------------------------------------------------------------------------
# playlist_links
# ---------------------------------------------------------------------------
playlist_links = sa.Table(
    "playlist_links",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "local_playlist_id",
        sa.Integer,
        sa.ForeignKey("playlists.id", ondelete="CASCADE"),
    ),
    sa.Column("service", sa.Text, nullable=False),
    sa.Column("service_playlist_id", sa.Text, nullable=False),
    sa.Column("service_playlist_name", sa.Text),
    sa.Column("sync_direction", sa.Text, server_default="pull"),
    sa.Column("sync_interval_minutes", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_synced_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# playlist_snapshots
# ---------------------------------------------------------------------------
playlist_snapshots = sa.Table(
    "playlist_snapshots",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("source_service", sa.Text, nullable=False),
    sa.Column("source_playlist_id", sa.Text, nullable=False),
    sa.Column("playlist_name", sa.Text),
    sa.Column("track_count", sa.Integer, server_default="0"),
    sa.Column("snapshot_data", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# sync_link_snapshots (delta detection for bidirectional sync)
# ---------------------------------------------------------------------------
sync_link_snapshots = sa.Table(
    "sync_link_snapshots",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "playlist_link_id",
        sa.Integer,
        sa.ForeignKey("playlist_links.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("side", sa.Text, nullable=False),  # 'local' or 'remote'
    sa.Column("tracks_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)

sa.Index(
    "idx_sync_link_snapshots_link",
    sync_link_snapshots.c.playlist_link_id,
    sync_link_snapshots.c.side,
)

# ---------------------------------------------------------------------------
# sync_schedules
# ---------------------------------------------------------------------------
sync_schedules = sa.Table(
    "sync_schedules",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "playlist_link_id",
        sa.Integer,
        sa.ForeignKey("playlist_links.id", ondelete="CASCADE"),
    ),
    sa.Column("interval_minutes", sa.Integer, server_default="60"),
    sa.Column("last_run_at", sa.DateTime),
    sa.Column("next_run_at", sa.DateTime),
    sa.Column("enabled", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# transfer_history
# ---------------------------------------------------------------------------
transfer_history = sa.Table(
    "transfer_history",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("operation", sa.Text, nullable=False),
    sa.Column("source_service", sa.Text),
    sa.Column("source_playlist_id", sa.Text),
    sa.Column("source_playlist_name", sa.Text),
    sa.Column("target_service", sa.Text),
    sa.Column("target_playlist_id", sa.Text),
    sa.Column("target_playlist_name", sa.Text),
    sa.Column("total_tracks", sa.Integer, server_default="0"),
    sa.Column("matched", sa.Integer, server_default="0"),
    sa.Column("approximate", sa.Integer, server_default="0"),
    sa.Column("not_found", sa.Integer, server_default="0"),
    sa.Column("status", sa.Text, server_default="pending"),
    sa.Column("details", sa.Text),
    sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("completed_at", sa.DateTime),
)

# ---------------------------------------------------------------------------
# zone_groups
# ---------------------------------------------------------------------------
zone_groups = sa.Table(
    "zone_groups",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column(
        "leader_zone_id",
        sa.Integer,
        sa.ForeignKey("zones.id", ondelete="SET NULL"),
    ),
    sa.Column("master_volume", sa.Float, server_default="0.5"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# zone_group_members  (composite PK, no auto-increment)
# ---------------------------------------------------------------------------
zone_group_members = sa.Table(
    "zone_group_members",
    metadata,
    sa.Column(
        "group_id",
        sa.Integer,
        sa.ForeignKey("zone_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "zone_id",
        sa.Integer,
        sa.ForeignKey("zones.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("volume_offset", sa.Float, server_default="0.0"),
    sa.Column("muted", sa.Boolean, server_default=sa.text("FALSE")),
)

# ---------------------------------------------------------------------------
# party_votes
# ---------------------------------------------------------------------------
party_votes = sa.Table(
    "party_votes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("zone_id", sa.Integer, nullable=False),
    sa.Column("track_title", sa.Text, nullable=False),
    sa.Column("track_artist", sa.Text),
    sa.Column("queue_position", sa.Integer, nullable=False),
    sa.Column("vote_count", sa.Integer, server_default="1"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_party_votes_zone", party_votes.c.zone_id)

# ---------------------------------------------------------------------------
# album_ratings
# ---------------------------------------------------------------------------
album_ratings = sa.Table(
    "album_ratings",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("album_id", sa.Integer, nullable=False),
    sa.Column("profile_id", sa.Integer),
    sa.Column("rating", sa.Integer),
    sa.Column("note", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# collaborative_playlists
# ---------------------------------------------------------------------------
collaborative_playlists = sa.Table(
    "collaborative_playlists",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("created_by", sa.Integer),
    sa.Column("is_public", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# collaborative_playlist_tracks
# ---------------------------------------------------------------------------
collaborative_playlist_tracks = sa.Table(
    "collaborative_playlist_tracks",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "playlist_id",
        sa.Integer,
        sa.ForeignKey("collaborative_playlists.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("track_id", sa.Integer),
    sa.Column("track_title", sa.Text, nullable=False),
    sa.Column("track_artist", sa.Text),
    sa.Column("added_by", sa.Integer),
    sa.Column("added_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("votes", sa.Integer, server_default="0"),
)

# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------
collections = sa.Table(
    "collections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("icon", sa.Text, server_default="folder"),
    sa.Column("color", sa.Text, server_default="#6366f1"),
    sa.Column("profile_id", sa.Integer),
    sa.Column("sort_order", sa.Integer, server_default="0"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# collection_albums
# ---------------------------------------------------------------------------
collection_albums = sa.Table(
    "collection_albums",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "collection_id",
        sa.Integer,
        sa.ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("album_id", sa.Integer, nullable=False),
    sa.Column("added_at", sa.DateTime, server_default=sa.func.now()),
    sa.UniqueConstraint("collection_id", "album_id"),
)

# ---------------------------------------------------------------------------
# zone_profiles
# ---------------------------------------------------------------------------
zone_profiles = sa.Table(
    "zone_profiles",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("config", sa.Text),
    sa.Column("icon", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# zone_audio_profiles (room correction / per-zone EQ)
# ---------------------------------------------------------------------------
zone_audio_profiles = sa.Table(
    "zone_audio_profiles",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("zone_id", sa.Integer, nullable=False),
    sa.Column("name", sa.Text, nullable=False, server_default="Default"),
    sa.Column("eq_preset", sa.Text),
    sa.Column("bass_boost", sa.Float, server_default="0"),
    sa.Column("treble_boost", sa.Float, server_default="0"),
    sa.Column("loudness_compensation", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("crossfeed", sa.Text),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.UniqueConstraint("zone_id", "name"),
)

# ---------------------------------------------------------------------------
# playback_history — recorded by the EventBus on every track play. Was only
# created by the legacy aiosqlite engine; missing on fresh SA installs which
# crashed with 'no such table' on first record. Mirror the engine.py schema.
# ---------------------------------------------------------------------------
playback_history = sa.Table(
    "playback_history",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer),
    sa.Column("track_id", sa.Integer, sa.ForeignKey("tracks.id", ondelete="SET NULL")),
    sa.Column("zone_id", sa.Integer),
    sa.Column("track_title", sa.Text),
    sa.Column("artist_name", sa.Text),
    sa.Column("album_title", sa.Text),
    sa.Column("cover_path", sa.Text),
    sa.Column("duration_ms", sa.Integer),
    sa.Column("listened_ms", sa.Integer),
    sa.Column("source", sa.Text),
    sa.Column("played_at", sa.DateTime, server_default=sa.func.now()),
    sa.Index("idx_playback_history_played", sa.text("played_at DESC")),
    # Composite indexes for the dashboard endpoint — every WHERE clause
    # filters by `played_at > cutoff` plus optionally one of these
    # dimensions. Without composite indexes the filtered queries fall
    # back to a full scan on libraries with 50k+ rows. SA reflection
    # adds them on next start.
    sa.Index("idx_playback_history_user_played", sa.text("user_id, played_at DESC")),
    sa.Index("idx_playback_history_zone_played", sa.text("zone_id, played_at DESC")),
    sa.Index("idx_playback_history_artist_played", sa.text("artist_name, played_at DESC")),
    sa.Index("idx_playback_history_source_played", sa.text("source, played_at DESC")),
)



# ---------------------------------------------------------------------------
# smart_collections — auto-rule-based album collections (v0.8.0 POC).
# Sister of `smart_playlists` but at album scope: a Smart Collection is
# a saved set of rules over the `albums` table (+ join-driven rules
# over `track_credits` for "engineered by Rudy Van Gelder" etc.).
# Rules stored as JSON text — same shape as smart_playlists.rules so
# the front-end builder UI can be reused. No materialisation table:
# membership is recomputed per GET via SQL compiled from rules,
# cached in-process for 30 s and invalidated on `library.scan_completed`
# / `playback.track_completed` events.
# ---------------------------------------------------------------------------
smart_collections = sa.Table(
    "smart_collections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("icon", sa.Text, server_default="folder"),
    sa.Column("color", sa.Text, server_default="#6366f1"),
    sa.Column("rules", sa.Text, nullable=False),  # JSON: list[Rule]
    sa.Column("match_mode", sa.Text, server_default="all"),  # 'all' | 'any'
    sa.Column("sort_by", sa.Text, server_default="added_at"),
    sa.Column("sort_order", sa.Text, server_default="desc"),
    sa.Column("max_albums", sa.Integer, server_default="500"),
    sa.Column("auto_refresh", sa.Integer, server_default="1"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

smart_playlists = sa.Table(
    "smart_playlists",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("rules", sa.Text, nullable=False),  # JSON: list[Rule]
    sa.Column("match_mode", sa.Text, server_default="all"),  # 'all' | 'any'
    sa.Column("sort_by", sa.Text, server_default="title"),
    sa.Column("sort_order", sa.Text, server_default="asc"),
    sa.Column("max_tracks", sa.Integer, server_default="200"),
    sa.Column("auto_refresh", sa.Integer, server_default="1"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

# ---------------------------------------------------------------------------
# alarms (scheduled playback / wake-up)
# ---------------------------------------------------------------------------
alarms = sa.Table(
    "alarms",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("time", sa.Text, nullable=False),  # HH:MM
    sa.Column("days", sa.Text, server_default="1,2,3,4,5"),  # 0=Sun..6=Sat
    sa.Column("skip_holidays", sa.Integer, server_default="0"),
    sa.Column("holiday_country", sa.Text, server_default="FR"),
    sa.Column("zone_id", sa.Integer),
    sa.Column("source_type", sa.Text, nullable=False),  # playlist, radio, album, favorites
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_name", sa.Text),
    sa.Column("volume", sa.Integer, server_default="50"),
    sa.Column("fade_in_seconds", sa.Integer, server_default="30"),
    sa.Column("enabled", sa.Integer, server_default="1"),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)
