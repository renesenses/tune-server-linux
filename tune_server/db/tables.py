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
    sa.Column("label", sa.Text),
    sa.Column("catalog_number", sa.Text),
    sa.Column("barcode", sa.Text),
    sa.Column("format", sa.Text),
    sa.Column("sample_rate", sa.Integer),
    sa.Column("bit_depth", sa.Integer),
    sa.Column("artist_name", sa.Text),
)

sa.Index("idx_albums_title", albums.c.title)
sa.Index("idx_albums_artist_id", albums.c.artist_id)
sa.Index("idx_albums_year", albums.c.year)
sa.Index("idx_albums_source", albums.c.source, albums.c.source_id)
sa.Index("idx_albums_created_at", albums.c.created_at)
sa.Index("idx_albums_genre", albums.c.genre)

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
    sa.Column("track_number", sa.Integer, server_default="0"),
    sa.Column("duration_ms", sa.Integer, server_default="0"),
    sa.Column("file_path", sa.Text, unique=True),
    sa.Column("format", sa.Text),
    sa.Column("sample_rate", sa.Integer),
    sa.Column("bit_depth", sa.Integer),
    sa.Column("channels", sa.Integer, server_default="2"),
    sa.Column("file_mtime", sa.Float),
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
    sa.Column("comment", sa.Text),
    sa.Column("musicbrainz_recording_id", sa.Text),
    sa.Column("acoustid", sa.Text),
    sa.Column("bpm", sa.Float),
    sa.Column("label", sa.Text),
    sa.Column("custom_tags", sa.Text),
    sa.Column("album_title", sa.Text),
    sa.Column("artist_name", sa.Text),
    sa.Column("cover_path", sa.Text),
)

sa.Index("idx_tracks_album_id", tracks.c.album_id)
sa.Index("idx_tracks_artist_id", tracks.c.artist_id)
sa.Index("idx_tracks_file_path", tracks.c.file_path)
sa.Index("idx_tracks_source", tracks.c.source, tracks.c.source_id)
sa.Index("idx_tracks_created_at", tracks.c.created_at)
sa.Index("idx_tracks_format_sr", tracks.c.format, tracks.c.sample_rate)
sa.Index("idx_tracks_audio_hash", tracks.c.audio_hash)

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
# users — application users (auth-ready, no auth implemented yet)
# ---------------------------------------------------------------------------
users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("username", sa.Text, nullable=False, unique=True),
    sa.Column("email", sa.Text, unique=True),
    sa.Column("display_name", sa.Text),
    sa.Column("password_hash", sa.Text),  # bcrypt hash, NULL if no auth
    sa.Column("role", sa.Text, nullable=False, server_default="user"),  # admin, user, guest
    sa.Column("avatar_color", sa.Text, server_default="'#FF6B35'"),
    sa.Column("preferences", sa.Text),  # JSON: theme, language, default_zone, etc.
    sa.Column("default_zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL")),
    sa.Column("last_login_at", sa.DateTime),
    sa.Column("is_active", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_users_username", users.c.username)
sa.Index("idx_users_email", users.c.email)

# ---------------------------------------------------------------------------
# user_sessions — JWT/API tokens, device tracking
# ---------------------------------------------------------------------------
user_sessions = sa.Table(
    "user_sessions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),  # SHA256 of JWT/API token
    sa.Column("device_id", sa.Integer, sa.ForeignKey("user_devices.id", ondelete="SET NULL")),
    sa.Column("ip_address", sa.Text),
    sa.Column("user_agent", sa.Text),
    sa.Column("expires_at", sa.DateTime),
    sa.Column("revoked", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("last_used_at", sa.DateTime),
)

sa.Index("idx_sessions_user", user_sessions.c.user_id)
sa.Index("idx_sessions_token", user_sessions.c.token_hash)

# ---------------------------------------------------------------------------
# user_devices — registered devices per user
# ---------------------------------------------------------------------------
user_devices = sa.Table(
    "user_devices",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),  # "iPhone de Bertrand", "MacBook Pro"
    sa.Column("device_type", sa.Text),  # mobile, desktop, tablet, tv, watch, web
    sa.Column("platform", sa.Text),  # ios, android, macos, windows, linux, web
    sa.Column("app_version", sa.Text),
    sa.Column("push_token", sa.Text),  # FCM/APNs push notification token
    sa.Column("last_seen_at", sa.DateTime),
    sa.Column("is_active", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_user_devices_user", user_devices.c.user_id)

# ---------------------------------------------------------------------------
# playback_history — scrobble-style listening history per user
# ---------------------------------------------------------------------------
playback_history = sa.Table(
    "playback_history",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("track_id", sa.Integer, sa.ForeignKey("tracks.id", ondelete="SET NULL")),
    sa.Column("zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL")),
    sa.Column("track_title", sa.Text),  # denormalized for deleted tracks
    sa.Column("artist_name", sa.Text),
    sa.Column("album_title", sa.Text),
    sa.Column("cover_path", sa.Text),
    sa.Column("duration_ms", sa.Integer),
    sa.Column("listened_ms", sa.Integer),  # how long they actually listened
    sa.Column("source", sa.Text),  # local, tidal, qobuz, radio, etc.
    sa.Column("played_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_history_user", playback_history.c.user_id)
sa.Index("idx_history_track", playback_history.c.track_id)
sa.Index("idx_history_played", playback_history.c.played_at)

# ---------------------------------------------------------------------------
# listening_stats — aggregated stats per user (materialized/cached)
# ---------------------------------------------------------------------------
listening_stats = sa.Table(
    "listening_stats",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("period", sa.Text, nullable=False),  # day, week, month, year, all
    sa.Column("period_start", sa.DateTime),
    sa.Column("artist_id", sa.Integer, sa.ForeignKey("artists.id", ondelete="SET NULL")),
    sa.Column("album_id", sa.Integer, sa.ForeignKey("albums.id", ondelete="SET NULL")),
    sa.Column("genre", sa.Text),
    sa.Column("play_count", sa.Integer, server_default="0"),
    sa.Column("total_ms", sa.Integer, server_default="0"),  # total listening time
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_stats_user_period", listening_stats.c.user_id, listening_stats.c.period)

# ---------------------------------------------------------------------------
# api_keys — API access tokens for third-party integrations
# ---------------------------------------------------------------------------
api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("name", sa.Text, nullable=False),  # "Home Assistant", "Shortcut iOS"
    sa.Column("key_hash", sa.Text, nullable=False, unique=True),  # SHA256 of API key
    sa.Column("key_prefix", sa.Text),  # first 8 chars for identification: "tune_abc1..."
    sa.Column("scopes", sa.Text),  # JSON array: ["read", "write", "playback", "admin"]
    sa.Column("last_used_at", sa.DateTime),
    sa.Column("expires_at", sa.DateTime),
    sa.Column("is_active", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_api_keys_hash", api_keys.c.key_hash)

# ---------------------------------------------------------------------------
# notifications — in-app notifications
# ---------------------------------------------------------------------------
notifications = sa.Table(
    "notifications",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("type", sa.Text, nullable=False),  # scan_complete, update_available, import_done, error
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("message", sa.Text),
    sa.Column("data", sa.Text),  # JSON payload (track_id, album_id, error details, etc.)
    sa.Column("read", sa.Boolean, server_default=sa.text("FALSE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_notifications_user", notifications.c.user_id)
sa.Index("idx_notifications_unread", notifications.c.user_id, notifications.c.read)

# ---------------------------------------------------------------------------
# tags — custom tags on albums/tracks/artists
# ---------------------------------------------------------------------------
tags = sa.Table(
    "tags",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("color", sa.Text),  # hex color for UI
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_tags_name", tags.c.name)

tag_items = sa.Table(
    "tag_items",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
    sa.Column("track_id", sa.Integer, sa.ForeignKey("tracks.id", ondelete="CASCADE")),
    sa.Column("album_id", sa.Integer, sa.ForeignKey("albums.id", ondelete="CASCADE")),
    sa.Column("artist_id", sa.Integer, sa.ForeignKey("artists.id", ondelete="CASCADE")),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
)

sa.Index("idx_tag_items_tag", tag_items.c.tag_id)

# ---------------------------------------------------------------------------
# smart_playlists — dynamic playlists with rules
# ---------------------------------------------------------------------------
smart_playlists = sa.Table(
    "smart_playlists",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE")),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("rules", sa.Text, nullable=False),  # JSON: [{"field":"genre","op":"eq","value":"Jazz"},{"field":"year","op":"gt","value":2000}]
    sa.Column("match_mode", sa.Text, server_default="all"),  # all (AND) / any (OR)
    sa.Column("sort_by", sa.Text, server_default="title"),  # title, artist, year, random, recently_added
    sa.Column("sort_order", sa.Text, server_default="asc"),  # asc, desc
    sa.Column("max_tracks", sa.Integer),  # limit, NULL = unlimited
    sa.Column("auto_refresh", sa.Boolean, server_default=sa.text("TRUE")),
    sa.Column("last_refreshed_at", sa.DateTime),
    sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
)
