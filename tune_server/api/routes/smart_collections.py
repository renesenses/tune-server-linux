"""Smart Collections REST surface (v0.8.0).

Mirrors the legacy /library/collections endpoints but membership is
rule-driven instead of manual. Rules grammar + compiler live in
``tune_server.library.smart_collection``.

  GET    /api/v1/library/smart-collections
  POST   /api/v1/library/smart-collections        (create)
  GET    /api/v1/library/smart-collections/{id}
  PUT    /api/v1/library/smart-collections/{id}   (update, rules included)
  DELETE /api/v1/library/smart-collections/{id}
  GET    /api/v1/library/smart-collections/{id}/albums  (computed membership)
  POST   /api/v1/library/smart-collections/{id}/preview (compile rules
        without persisting — used by the rule builder UI to show
        live counts as the user edits)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tune_server.api.deps import deps
from tune_server.library.smart_collection import (
    SmartCollectionRepo,
    compile_rules,
)

router = APIRouter(prefix="/library/smart-collections", tags=["smart-collections"])


class SmartCollectionCreate(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = "folder"
    color: str | None = "#6366f1"
    rules: list[dict] = []
    match_mode: str = "all"
    sort_by: str = "added_at"
    sort_order: str = "desc"
    max_albums: int = 500
    auto_refresh: bool = True


class SmartCollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    rules: list[dict] | None = None
    match_mode: str | None = None
    sort_by: str | None = None
    sort_order: str | None = None
    max_albums: int | None = None
    auto_refresh: bool | None = None


class PreviewRequest(BaseModel):
    rules: list[dict] = []
    match_mode: str = "all"
    sort_by: str = "added_at"
    sort_order: str = "desc"
    max_albums: int = 500


def _repo() -> SmartCollectionRepo:
    if deps.db is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    return SmartCollectionRepo(deps.db)


@router.get("")
async def list_smart_collections() -> list[dict]:
    return await _repo().list()


@router.post("")
async def create_smart_collection(body: SmartCollectionCreate) -> dict:
    repo = _repo()
    sc_id = await repo.create(body.model_dump())
    created = await repo.get(sc_id)
    return created or {"id": sc_id}


@router.get("/{collection_id}")
async def get_smart_collection(collection_id: int) -> dict:
    sc = await _repo().get(collection_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="smart_collection_not_found")
    return sc


@router.put("/{collection_id}")
async def update_smart_collection(
    collection_id: int, body: SmartCollectionUpdate,
) -> dict:
    repo = _repo()
    existing = await repo.get(collection_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="smart_collection_not_found")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    await repo.update(collection_id, payload)
    return await repo.get(collection_id) or {"updated": True}


@router.delete("/{collection_id}")
async def delete_smart_collection(collection_id: int) -> dict:
    await _repo().delete(collection_id)
    return {"deleted": True}


@router.get("/{collection_id}/albums")
async def smart_collection_albums(collection_id: int) -> list[dict]:
    repo = _repo()
    if await repo.get(collection_id) is None:
        raise HTTPException(status_code=404, detail="smart_collection_not_found")
    return await repo.evaluate(collection_id)


@router.post("/preview")
async def preview_smart_collection(body: PreviewRequest) -> dict:
    """Compile rules without persisting and return the matching albums.
    Lets the rule-builder UI show live "X albums match" counts as the
    user edits a Smart Collection without round-tripping create/delete."""
    if deps.db is None:
        raise HTTPException(status_code=503, detail="db_unavailable")
    try:
        sql, params = compile_rules(
            body.rules,
            match_mode=body.match_mode,
            sort_by=body.sort_by,
            sort_order=body.sort_order,
            max_albums=body.max_albums,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_rules: {exc}")
    try:
        rows = await deps.db.fetchall(sql, tuple(params))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"rules_eval_failed: {exc}")
    albums = [dict(r) for r in rows]
    return {"count": len(albums), "albums": albums}
