from __future__ import annotations

from tune_server.db.engine import DatabaseProtocol, SQLiteDatabase


def create_database(config) -> DatabaseProtocol:
    """Factory: return the right database implementation based on config."""
    engine = getattr(config, "db_engine", "sqlite")
    if engine == "postgres":
        from tune_server.db.postgres import PostgresDatabase

        db_url = getattr(config, "db_url", None)
        if not db_url:
            raise ValueError("TUNE_DB_URL is required when TUNE_DB_ENGINE='postgres'")
        return PostgresDatabase(db_url)
    return SQLiteDatabase(config.db_path)
