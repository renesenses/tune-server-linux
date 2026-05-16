"""Plugin Store API — browse, install, uninstall, update plugins."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from tune_server.api import deps

router = APIRouter(prefix="/store", tags=["plugin-store"])


@router.get("/plugins")
async def list_store_plugins():
    """Merged list of available + installed plugins."""
    if not deps.store_manager:
        raise HTTPException(503, "Plugin store not available")
    return await deps.store_manager.list_merged(deps.plugin_loader)


@router.post("/plugins/{name}/install")
async def install_plugin(name: str):
    """Install a plugin from the catalog via pip."""
    if not deps.store_manager:
        raise HTTPException(503, "Plugin store not available")
    result = await deps.store_manager.install(name)
    if not result["success"]:
        raise HTTPException(400, result.get("error") or result.get("message"))
    return result


@router.delete("/plugins/{name}")
async def uninstall_plugin(name: str):
    """Uninstall an installed plugin."""
    if not deps.store_manager:
        raise HTTPException(503, "Plugin store not available")
    result = await deps.store_manager.uninstall(name)
    if not result["success"]:
        raise HTTPException(400, result.get("error") or result.get("message"))
    return result


@router.post("/plugins/{name}/update")
async def update_plugin(name: str):
    """Update an installed plugin to the latest version."""
    if not deps.store_manager:
        raise HTTPException(503, "Plugin store not available")
    result = await deps.store_manager.update(name)
    if not result["success"]:
        raise HTTPException(400, result.get("error") or result.get("message"))
    return result


@router.get("/docs", response_class=PlainTextResponse)
async def plugin_dev_docs():
    """Serve the plugin development guide as markdown."""
    doc_path = Path(__file__).resolve().parents[3] / "docs" / "plugin-development.md"
    if not doc_path.is_file():
        raise HTTPException(404, "Plugin development guide not found")
    return doc_path.read_text(encoding="utf-8")
