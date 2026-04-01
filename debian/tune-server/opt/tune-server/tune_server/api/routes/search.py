from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Query

from tune_server.api.deps import deps
from tune_server.db.repository import full_text_search
from tune_server.models import FederatedSearchResult, SearchResult

logger = structlog.get_logger()

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=FederatedSearchResult)
async def federated_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    sources: str | None = Query(None),  # comma-separated: "local,tidal,youtube"
):
    """Search across local library and streaming services in parallel."""
    # Determine which sources to query
    if sources:
        requested = {s.strip() for s in sources.split(",") if s.strip()}
    else:
        requested = {"local"} | set(deps.streaming_services.keys())

    result = FederatedSearchResult()

    # Build tasks
    tasks: dict[str, asyncio.Task] = {}

    if "local" in requested and deps.db:
        tasks["local"] = asyncio.ensure_future(
            full_text_search(deps.db, q, limit=limit)
        )

    for name, service in deps.streaming_services.items():
        if name in requested and service.is_authenticated:
            tasks[name] = asyncio.ensure_future(service.search(q, limit=limit))

    if tasks:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for key, res in zip(tasks.keys(), results):
            if isinstance(res, Exception):
                logger.warning("federated_search_service_error", service=key, error=str(res))
                continue
            if key == "local":
                result.local = res
            else:
                result.services[key] = res

    return result
