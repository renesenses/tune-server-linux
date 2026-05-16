"""Plugin Store API — browse, install, uninstall, update plugins."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

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
