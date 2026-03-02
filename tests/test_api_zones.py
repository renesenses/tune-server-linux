from __future__ import annotations

import pytest

from tune_server.models import OutputType


async def test_list_zones_empty(app_client_with_zones):
    resp = await app_client_with_zones.get("/api/v1/zones")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_zone(app_client_with_zones):
    resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Living Room", "output_type": "local"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Living Room"
    assert data["output_type"] == "local"
    assert data["id"] is not None


async def test_get_zone(app_client_with_zones):
    # Create first
    create_resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Kitchen", "output_type": "local"},
    )
    zone_id = create_resp.json()["id"]

    resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Kitchen"


async def test_get_zone_404(app_client_with_zones):
    resp = await app_client_with_zones.get("/api/v1/zones/999")
    assert resp.status_code == 404


async def test_delete_zone(app_client_with_zones):
    create_resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Temp Zone", "output_type": "local"},
    )
    zone_id = create_resp.json()["id"]

    resp = await app_client_with_zones.delete(f"/api/v1/zones/{zone_id}")
    assert resp.status_code == 204

    # Verify deleted
    resp = await app_client_with_zones.get(f"/api/v1/zones/{zone_id}")
    assert resp.status_code == 404


async def test_delete_zone_404(app_client_with_zones):
    resp = await app_client_with_zones.delete("/api/v1/zones/999")
    assert resp.status_code == 404


async def test_group_zones(app_client_with_zones):
    r1 = await app_client_with_zones.post("/api/v1/zones", json={"name": "Leader", "output_type": "local"})
    r2 = await app_client_with_zones.post("/api/v1/zones", json={"name": "Follower", "output_type": "local"})
    lid = r1.json()["id"]
    fid = r2.json()["id"]

    resp = await app_client_with_zones.post(
        "/api/v1/zones/group",
        json={"leader_id": lid, "zone_ids": [lid, fid]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "group_id" in data
    assert data["leader_id"] == lid


async def test_group_zones_leader_404(app_client_with_zones):
    resp = await app_client_with_zones.post(
        "/api/v1/zones/group",
        json={"leader_id": 999, "zone_ids": [999, 998]},
    )
    assert resp.status_code == 404


async def test_ungroup_zones(app_client_with_zones):
    r1 = await app_client_with_zones.post("/api/v1/zones", json={"name": "Leader", "output_type": "local"})
    r2 = await app_client_with_zones.post("/api/v1/zones", json={"name": "Follower", "output_type": "local"})
    lid = r1.json()["id"]
    fid = r2.json()["id"]

    group_resp = await app_client_with_zones.post(
        "/api/v1/zones/group",
        json={"leader_id": lid, "zone_ids": [lid, fid]},
    )
    group_id = group_resp.json()["group_id"]

    resp = await app_client_with_zones.delete(f"/api/v1/zones/group/{group_id}")
    assert resp.status_code == 204


async def test_list_groups(app_client_with_zones):
    resp = await app_client_with_zones.get("/api/v1/zones/groups/list")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_zone_with_sync_delay(app_client_with_zones):
    resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Sync Zone", "output_type": "local", "sync_delay_ms": 150},
    )
    assert resp.status_code == 201
    assert resp.json()["sync_delay_ms"] == 150


async def test_update_zone_sync_delay(app_client_with_zones):
    create_resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Update Zone", "output_type": "local"},
    )
    zone_id = create_resp.json()["id"]

    resp = await app_client_with_zones.put(
        f"/api/v1/zones/{zone_id}",
        json={"name": "Update Zone", "sync_delay_ms": -200},
    )
    assert resp.status_code == 200
    assert resp.json()["sync_delay_ms"] == -200


async def test_patch_zone_sync_delay(app_client_with_zones):
    create_resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Patch Zone", "output_type": "local"},
    )
    zone_id = create_resp.json()["id"]

    resp = await app_client_with_zones.patch(
        f"/api/v1/zones/{zone_id}",
        json={"sync_delay_ms": 300},
    )
    assert resp.status_code == 200
    assert resp.json()["sync_delay_ms"] == 300


async def test_create_zone_default_sync_delay(app_client_with_zones):
    resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Default Zone", "output_type": "local"},
    )
    assert resp.status_code == 201
    assert resp.json()["sync_delay_ms"] == 0
