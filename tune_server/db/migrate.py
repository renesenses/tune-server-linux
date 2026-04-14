"""Migrate Tune Server database from SQLite to PostgreSQL (or vice versa).

Usage:
    python -m tune_server.db.migrate --from sqlite --to postgres
    python -m tune_server.db.migrate --from postgres --to sqlite

Environment:
    TUNE_DB_PATH     SQLite database path (default: tune_server.db)
    TUNE_DB_URL      PostgreSQL connection URL
"""
from __future__ import annotations

import asyncio
import sys

import structlog

logger = structlog.get_logger()


def _convert_value(value, column: str, ts_columns: set[str], target_engine: str):
    """Convert values between SQLite and PostgreSQL types."""
    if value is None:
        return value
    # SQLite stores timestamps as strings; PostgreSQL needs datetime objects
    if target_engine == "postgres" and column in ts_columns and isinstance(value, str):
        from datetime import datetime
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return value  # Return as-is if no format matches
    # PostgreSQL→SQLite: datetime to string
    if target_engine == "sqlite" and column in ts_columns and not isinstance(value, str):
        return str(value)
    # PostgreSQL doesn't accept null bytes in text
    if target_engine == "postgres" and isinstance(value, str) and "\x00" in value:
        return value.replace("\x00", "")
    # SQLite stores booleans as 0/1; PostgreSQL needs real bools
    if target_engine == "postgres" and column in _BOOL_COLUMNS and isinstance(value, int):
        return bool(value)
    return value


_BOOL_COLUMNS = {
    "favorite", "muted", "online", "auto_mount", "enabled", "resolved",
    "is_current",
}


# Tables in dependency order (foreign keys)
TABLES_ORDERED = [
    "artists",
    "albums",
    "tracks",
    "playlists",
    "playlist_tracks",
    "zones",
    "play_queue",
    "streaming_auth",
    "radio_favorites",
    "radio_stations",
    "user_profiles",
    "user_favorites",
    "device_credentials",
    "network_mounts",
    "duplicate_tracks",
    "metadata_fix_reports",
    "metadata_suggestions",
    "playlist_links",
    "playlist_snapshots",
    "sync_schedules",
    "transfer_history",
    "zone_groups",
    "zone_group_members",
    "zone_profiles",
]

# Tables with SERIAL/AUTOINCREMENT ids that need sequence reset
SERIAL_TABLES = [
    "artists", "albums", "tracks", "playlists", "playlist_tracks",
    "zones", "play_queue", "radio_favorites", "radio_stations",
    "user_profiles", "user_favorites", "duplicate_tracks",
    "metadata_fix_reports", "metadata_suggestions", "network_mounts",
    "playlist_links", "playlist_snapshots", "sync_schedules",
    "transfer_history", "zone_groups", "zone_profiles",
]


async def migrate(source_engine: str, target_engine: str) -> None:
    from tune_server.config import settings
    from tune_server.db.engine import SQLiteDatabase
    from tune_server.db.postgres import PostgresDatabase

    if source_engine == target_engine:
        print(f"Source and target are both '{source_engine}'. Nothing to do.")
        return

    # Connect to source
    print(f"Connecting to source ({source_engine})...")
    if source_engine == "sqlite":
        source = SQLiteDatabase(settings.db_path)
    else:
        source = PostgresDatabase(settings.db_url)
    await source.connect()

    # Connect to target
    print(f"Connecting to target ({target_engine})...")
    if target_engine == "sqlite":
        target = SQLiteDatabase(settings.db_path.replace(".db", "_migrated.db"))
    else:
        target = PostgresDatabase(settings.db_url)
    await target.connect()

    total_rows = 0

    try:
        for table in TABLES_ORDERED:
            # Check if table exists in source
            if source_engine == "sqlite":
                check = await source.fetchone(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
            else:
                check = await source.fetchone(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=?",
                    (table,),
                )
            if not check:
                print(f"  {table}: skipped (not in source)")
                continue

            # Read all rows from source
            rows = await source.fetchall(f"SELECT * FROM {table}")
            if not rows:
                print(f"  {table}: 0 rows")
                continue

            # Get column names from first row
            columns = list(rows[0].keys())

            # Clear target table
            await target.execute(f"DELETE FROM {table}")
            await target.commit()

            # Insert rows into target
            placeholders = ", ".join(["?"] * len(columns))
            col_names = ", ".join(columns)
            insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

            # Detect timestamp columns for string→datetime conversion
            ts_columns = {c for c in columns if c.endswith("_at") or c == "saved_at"}

            batch_size = 500
            count = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                params_seq = [tuple(_convert_value(row[c], c, ts_columns, target_engine) for c in columns) for row in batch]
                await target.executemany(insert_sql, params_seq)
                await target.commit()
                count += len(batch)

            total_rows += count
            print(f"  {table}: {count} rows migrated")

        # Reset PostgreSQL sequences to max(id) + 1
        if target_engine == "postgres":
            print("Resetting PostgreSQL sequences...")
            for table in SERIAL_TABLES:
                try:
                    row = await target.fetchone(f"SELECT MAX(id) as max_id FROM {table}")
                    max_id = row["max_id"] if row and row["max_id"] else 0
                    if max_id > 0:
                        # asyncpg needs raw SQL for setval
                        seq_name = f"{table}_id_seq"
                        await target.execute(
                            f"SELECT setval('{seq_name}', ?)",
                            (max_id,),
                        )
                except Exception as e:
                    logger.debug("sequence_reset_skip", table=table, error=str(e))

            await target.commit()

        print(f"\nMigration complete: {total_rows} total rows migrated.")
        if target_engine == "sqlite":
            db_path = settings.db_path.replace(".db", "_migrated.db")
            print(f"SQLite database written to: {db_path}")
            print(f"To use it: mv {db_path} {settings.db_path}")
        else:
            print("PostgreSQL database is ready.")
            print("Update your config: TUNE_DB_ENGINE=postgres")

    finally:
        await source.close()
        await target.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate Tune Server database between SQLite and PostgreSQL"
    )
    parser.add_argument(
        "--from", dest="source", required=True, choices=["sqlite", "postgres"],
        help="Source database engine",
    )
    parser.add_argument(
        "--to", dest="target", required=True, choices=["sqlite", "postgres"],
        help="Target database engine",
    )
    args = parser.parse_args()

    asyncio.run(migrate(args.source, args.target))


if __name__ == "__main__":
    main()
