"""SQLite backup utilities — standalone functions for file-based backup/restore.

These are SQLite-specific operations that don't belong in the engine abstraction.
Used by system routes and app startup.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()

_MAX_BACKUPS = 5


def create_backup(db_path: str) -> dict | None:
    """Create a timestamped backup of the SQLite database.

    Returns backup info dict or None on error.
    """
    db_file = Path(db_path)
    if not db_file.exists():
        return None

    backup_dir = db_file.parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_file.stem}_{timestamp}{db_file.suffix}"

    try:
        shutil.copy2(str(db_file), str(backup_path))
        # Copy WAL and SHM files if they exist
        for suffix in ("-wal", "-shm"):
            wal_file = db_file.with_name(db_file.name + suffix)
            if wal_file.exists():
                shutil.copy2(str(wal_file), str(backup_path.name) + suffix)
        logger.info("database_backup_created", path=str(backup_path))
    except Exception:
        logger.exception("database_backup_error")
        return None

    # Prune old backups
    _prune_backups(backup_dir, db_file.stem, db_file.suffix)

    stat = backup_path.stat()
    return {
        "filename": backup_path.name,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def list_backups(db_path: str) -> list[dict]:
    """List available backups, newest first."""
    db_file = Path(db_path)
    backup_dir = db_file.parent / "backups"
    if not backup_dir.exists():
        return []
    backups = sorted(backup_dir.glob(f"{db_file.stem}_*{db_file.suffix}"), reverse=True)
    return [
        {
            "filename": b.name,
            "size": b.stat().st_size,
            "created_at": datetime.fromtimestamp(b.stat().st_mtime).isoformat(),
        }
        for b in backups
    ]


def restore_backup(db_path: str, filename: str) -> bool:
    """Restore database from a backup file. Returns True on success.

    Caller is responsible for closing/reopening DB connections.
    """
    db_file = Path(db_path)
    backup_dir = db_file.parent / "backups"
    backup_path = backup_dir / filename

    if not backup_path.exists():
        return False

    # Validate: prevent path traversal
    if backup_path.parent.resolve() != backup_dir.resolve():
        return False

    try:
        # Remove WAL/SHM files
        for suffix in ("-wal", "-shm"):
            wal = db_file.with_name(db_file.name + suffix)
            if wal.exists():
                wal.unlink()

        shutil.copy2(str(backup_path), str(db_file))
        logger.info("database_restored", backup=filename)
        return True
    except Exception:
        logger.exception("database_restore_error", backup=filename)
        return False


def _prune_backups(backup_dir: Path, stem: str, suffix: str) -> None:
    """Keep only the last N backups."""
    backups = sorted(backup_dir.glob(f"{stem}_*{suffix}"))
    while len(backups) > _MAX_BACKUPS:
        old = backups.pop(0)
        try:
            old.unlink()
            for ext in ("-wal", "-shm"):
                wal = old.with_name(old.name + ext)
                if wal.exists():
                    wal.unlink()
            logger.info("database_backup_pruned", path=str(old))
        except Exception:
            pass
