"""FastAPI routes contributed by the example plugin.

All routes are mounted under /api/v1/ by PluginContext.register_router().
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["example"])


@router.get("/example/hello")
async def hello():
    """Simple health-check endpoint."""
    return {"plugin": "example", "status": "ok"}


@router.get("/example/echo/{message}")
async def echo(message: str):
    """Echo back a message — useful for testing."""
    return {"echo": message}
