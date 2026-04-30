from __future__ import annotations

from tune_server.db.engine import DatabaseProtocol, SQLiteDatabase


def create_database(config, use_sa: bool = False) -> DatabaseProtocol:
    """Factory: return the right database implementation based on config.

    Args:
        config: Application settings (db_engine, db_path, db_url, etc.)
        use_sa: If True, use the new SQLAlchemy Core engine (SADatabase).
                If False (default), use the legacy engine for backward compat.
    """
    engine = getattr(config, "db_engine", "sqlite")

    if use_sa:
        from tune_server.db.sa_engine import SADatabase

        return SADatabase(
            engine_name=engine,
            db_path=getattr(config, "db_path", None),
            db_url=getattr(config, "db_url", None),
            pool_min=getattr(config, "db_pool_min", 2),
            pool_max=getattr(config, "db_pool_max", 10),
        )

    # Legacy engines
    if engine == "postgres":
        from tune_server.db.postgres import PostgresDatabase

        db_url = getattr(config, "db_url", None)
        if not db_url:
            raise ValueError("TUNE_DB_URL is required when TUNE_DB_ENGINE='postgres'")
        pool_min = getattr(config, "db_pool_min", 2)
        pool_max = getattr(config, "db_pool_max", 10)
        return PostgresDatabase(db_url, pool_min=pool_min, pool_max=pool_max)
    return SQLiteDatabase(config.db_path)
