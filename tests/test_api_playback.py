from __future__ import annotations

import pytest

from tune_server.api.deps import deps


@pytest.fixture
async def zone_client(app_client_with_zones):
    """Client with a pre-created zone for playback tests."""
    resp = await app_client_with_zones.post(
        "/api/v1/zones",
        json={"name": "Playback Zone", "output_type": "local"},
    )
    zone_id = resp.json()["id"]
    return app_client_with_zones, zone_id


async def test_play_zone_not_found(app_client_with_zones):
    resp = await app_client_with_zones.post("/api/v1/zones/999/play")
    assert resp.status_code == 404


async def test_play_with_track_ids(zone_client):
    client, zone_id = zone_client
    # sample_tracks were created by the fixture, get their IDs
    tracks_resp = await client.get("/api/v1/library/tracks")
    track_ids = [t["id"] for t in tracks_resp.json()[:2]]

    resp = await client.post(
        f"/api/v1/zones/{zone_id}/play",
        json={"track_ids": track_ids},
    )
    # Will get a 200 even though playback may fail (mock output)
    # The important thing is it doesn't 404/500
    assert resp.status_code == 200


async def test_play_resume(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/play")
    assert resp.status_code == 200


async def test_pause(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/pause")
    assert resp.status_code == 200


async def test_resume(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/resume")
    assert resp.status_code == 200


async def test_stop(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/stop")
    assert resp.status_code == 200


async def test_next(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/next")
    assert resp.status_code == 200


async def test_previous(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/previous")
    assert resp.status_code == 200


async def test_volume(zone_client):
    client, zone_id = zone_client
    resp = await client.post(
        f"/api/v1/zones/{zone_id}/volume",
        json={"volume": 0.7},
    )
    assert resp.status_code == 200


async def test_get_queue(zone_client):
    client, zone_id = zone_client
    resp = await client.get(f"/api/v1/zones/{zone_id}/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert "tracks" in data
    assert "position" in data
    assert "length" in data


async def test_shuffle(zone_client):
    client, zone_id = zone_client
    resp = await client.post(f"/api/v1/zones/{zone_id}/shuffle?enabled=true")
    assert resp.status_code == 200
    assert "shuffle" in resp.json()


async def test_get_status(zone_client):
    client, zone_id = zone_client
    resp = await client.get(f"/api/v1/zones/{zone_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == zone_id
    assert "state" in data
