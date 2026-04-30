"""Metadata Manager API routes — edit, batch, write tags, suggestions, covers."""

from __future__ import annotations

from fastapi import APIRouter

from tune_server.api.routes.metadata.track_edits import router as track_edits_router
from tune_server.api.routes.metadata.batch import router as batch_router
from tune_server.api.routes.metadata.suggestions import router as suggestions_router
from tune_server.api.routes.metadata.covers import router as covers_router

router = APIRouter(prefix="/metadata", tags=["metadata"])

router.include_router(track_edits_router)
router.include_router(batch_router)
router.include_router(suggestions_router)
router.include_router(covers_router)
