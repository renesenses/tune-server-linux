"""Plugin Store API — unified endpoints for browsing, installing, uninstalling plugins.

Provides /api/v1/plugins endpoints that merge the remote catalog with locally
installed plugins, plus server-side pip install/uninstall.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tune_server.api.deps import deps

router = APIRouter(prefix="/plugins", tags=["plugins"])


@router.get("")
async def list_plugins():
    """Merged list of available catalog plugins + locally installed plugins.

    Each entry includes:
      - name, display_name, description, version, category, author
      - icon (URL or emoji hint)
      - installed (bool), installed_version, update_available
      - compatible (bool), status (available | active | disabled | error)
    """
    if deps.store_manager and deps.plugin_loader:
        return await deps.store_manager.list_merged(deps.plugin_loader)

    # Fallback: if store_manager is unavailable, return local plugins only
    if deps.plugin_loader:
        return deps.plugin_loader.list_plugins()

    return []


@router.get("/{slug}")
async def get_plugin_detail(slug: str):
    """Detailed info for a single plugin (catalog + local state)."""
    # Try merged catalog first
    if deps.store_manager and deps.plugin_loader:
        merged = await deps.store_manager.list_merged(deps.plugin_loader)
        for p in merged:
            if p.get("name") == slug:
                return p

    # Fallback to local plugin info
    if deps.plugin_loader:
        plugin = deps.plugin_loader.plugins.get(slug)
        if plugin:
            return {
                "name": slug,
                "display_name": slug,
                "version": getattr(plugin, "version", "?"),
                "description": getattr(plugin, "description", ""),
                "status": "active",
                "installed": True,
                "installed_version": getattr(plugin, "version", "?"),
            }
        error = deps.plugin_loader.failed.get(slug)
        if error is not None:
            return {
                "name": slug,
                "display_name": slug,
                "version": "?",
                "description": "",
                "status": "disabled" if error == "disabled" else "error",
                "installed": True,
                "error_message": error if error != "disabled" else None,
            }

    raise HTTPException(status_code=404, detail=f"Plugin '{slug}' not found")


@router.post("/{slug}/install")
async def install_plugin(slug: str):
    """Install a plugin from the catalog via pip.

    Returns {"success": true, "message": "...", "restart_required": true} on success.
    """
    if not deps.store_manager:
        raise HTTPException(status_code=503, detail="Plugin store not available")

    result = await deps.store_manager.install(slug)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or result.get("message") or "Install failed",
        )
    return result


@router.delete("/{slug}")
async def uninstall_plugin(slug: str):
    """Uninstall an installed plugin via pip.

    Returns {"success": true, "message": "...", "restart_required": true} on success.
    """
    if not deps.store_manager:
        raise HTTPException(status_code=503, detail="Plugin store not available")

    result = await deps.store_manager.uninstall(slug)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or result.get("message") or "Uninstall failed",
        )
    return result


@router.post("/{slug}/update")
async def update_plugin(slug: str):
    """Update an installed plugin to the latest version."""
    if not deps.store_manager:
        raise HTTPException(status_code=503, detail="Plugin store not available")

    result = await deps.store_manager.update(slug)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or result.get("message") or "Update failed",
        )
    return result
