"""Auto-fix engine — background scan + automatic metadata correction.

Scans the library for tracks with incomplete/incorrect metadata,
queries external sources, and either auto-applies high-confidence
corrections or stores them as suggestions for review.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import structlog

from tune_server.metadata_manager.enricher import enrich_track
from tune_server.event_bus import Event, EventBus

logger = structlog.get_logger()


class AutoFixEngine:
    """Background metadata auto-fix scanner."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._progress = {"status": "idle", "current": 0, "total": 0, "fixed": 0, "suggestions": 0}

    @property
    def status(self) -> dict:
        return dict(self._progress)

    async def start_scan(
        self,
        db,
        event_bus: EventBus | None = None,
        lastfm_key: str = "",
        discogs_token: str = "",
        cache_dir: str = "artwork_cache",
        auto_apply_threshold: float = 0.9,
        batch_size: int = 50,
    ) -> None:
        """Start a background metadata scan.

        Scans tracks missing genre, year, ISRC, or MusicBrainz ID.
        """
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(
            self._scan_loop(db, event_bus, lastfm_key, discogs_token,
                            cache_dir, auto_apply_threshold, batch_size)
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _scan_loop(
        self, db, event_bus, lastfm_key, discogs_token,
        cache_dir, auto_apply_threshold, batch_size,
    ) -> None:
        started_at = datetime.utcnow().isoformat()
        auto_fixed = 0
        suggestions = 0
        errors = 0

        try:
            # Find tracks needing enrichment
            incomplete = await db.fetchall("""
                SELECT t.id, t.title, t.artist_name, a.title as album_title
                FROM tracks t
                LEFT JOIN albums a ON a.id = t.album_id
                WHERE t.source = 'local'
                  AND (t.genre IS NULL OR t.isrc IS NULL
                       OR t.musicbrainz_recording_id IS NULL
                       OR t.year IS NULL)
                LIMIT ?
            """, (batch_size * 10,))

            total = len(incomplete)
            self._progress = {"status": "running", "current": 0, "total": total, "fixed": 0, "suggestions": 0}
            logger.info("autofix_started", total=total)

            if event_bus:
                event_bus.emit_nowait(Event(
                    type="metadata.autofix.started",
                    data={"total": total},
                    source="metadata_manager",
                ))

            for i, row in enumerate(incomplete):
                if not self._running:
                    break

                try:
                    result = await enrich_track(
                        track_id=row["id"],
                        title=row["title"],
                        artist=row["artist_name"] or "",
                        album=row["album_title"] or "",
                        db=db,
                        lastfm_key=lastfm_key,
                        discogs_token=discogs_token,
                        cache_dir=cache_dir,
                        auto_apply=True,
                        min_confidence=auto_apply_threshold,
                    )
                    auto_fixed += result.get("auto_applied", 0)
                    suggestions += result.get("suggestions", 0) - result.get("auto_applied", 0)
                except Exception:
                    errors += 1

                self._progress["current"] = i + 1
                self._progress["fixed"] = auto_fixed
                self._progress["suggestions"] = suggestions

                # Progress event every 10 tracks
                if event_bus and (i + 1) % 10 == 0:
                    event_bus.emit_nowait(Event(
                        type="metadata.autofix.progress",
                        data={"current": i + 1, "total": total,
                              "fixed": auto_fixed, "suggestions": suggestions},
                        source="metadata_manager",
                    ))

                # Rate limit (MusicBrainz: 1 req/s)
                await asyncio.sleep(1.2)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("autofix_error")
            errors += 1

        # Save report
        completed_at = datetime.utcnow().isoformat()
        try:
            await db.execute(
                """INSERT INTO metadata_fix_reports
                   (started_at, completed_at, tracks_scanned, auto_fixed, suggestions, errors, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (started_at, completed_at, self._progress.get("current", 0),
                 auto_fixed, suggestions, errors,
                 json.dumps({"threshold": auto_apply_threshold})),
            )
            await db.commit()
        except Exception:
            pass

        self._progress = {
            "status": "completed",
            "current": self._progress.get("current", 0),
            "total": self._progress.get("total", 0),
            "fixed": auto_fixed,
            "suggestions": suggestions,
            "errors": errors,
        }
        self._running = False

        if event_bus:
            event_bus.emit_nowait(Event(
                type="metadata.autofix.completed",
                data=self._progress,
                source="metadata_manager",
            ))

        logger.info("autofix_completed", fixed=auto_fixed, suggestions=suggestions, errors=errors)


# Singleton
_auto_fix_engine = AutoFixEngine()


def get_auto_fix_engine() -> AutoFixEngine:
    return _auto_fix_engine
