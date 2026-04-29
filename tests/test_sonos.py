"""Tests for the Sonos manager + output (v0.8.0).

Mock the SoCo discovery + per-speaker calls so the lifecycle, group
join/unjoin, and play_uri call shapes are exercised without needing
real Sonos hardware on the LAN.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.audio.formats import SONOS_CAPABILITIES
from tune_server.models import OutputType
from tune_server.outputs.sonos import SonosOutput
from tune_server.zones.sonos_manager import SonosManager, SonosSpeaker


def _make_fake_speaker(uid="RINCON_A", name="Living", ip="192.168.1.10",
                      coord_uid=None) -> MagicMock:
    sp = MagicMock()
    sp.uid = uid
    sp.player_name = name
    sp.ip_address = ip
    coord = MagicMock()
    coord.uid = coord_uid or uid
    grp = MagicMock()
    grp.coordinator = coord
    sp.group = grp
    sp.join = MagicMock()
    sp.unjoin = MagicMock()
    sp.play_uri = MagicMock()
    sp.stop = MagicMock()
    sp.pause = MagicMock()
    sp.play = MagicMock()
    sp.volume = 50
    return sp


# ---------------------------------------------------------------------------
# Static / config tests
# ---------------------------------------------------------------------------


def test_output_type_enum_includes_sonos():
    assert OutputType.SONOS.value == "sonos"


def test_capabilities_exposes_sonos_profile():
    # S2 ceiling — 48 kHz, 24-bit, FLAC/MP3/AAC/WAV.
    assert SONOS_CAPABILITIES.max_sample_rate == 48000
    assert SONOS_CAPABILITIES.max_bit_depth == 24


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_caches_found_speakers():
    mgr = SonosManager()
    speakers = {_make_fake_speaker("RINCON_A"), _make_fake_speaker("RINCON_B")}
    fake_soco = MagicMock()
    fake_soco.discover = MagicMock(return_value=speakers)
    with patch.dict("sys.modules", {"soco": fake_soco}):
        result = await mgr.discover(timeout_s=1)
    assert {s.uid for s in result} == {"RINCON_A", "RINCON_B"}
    assert mgr.get("RINCON_A") is not None
    assert mgr.get("RINCON_B") is not None


@pytest.mark.asyncio
async def test_discover_with_no_speakers_clears_cache():
    mgr = SonosManager()
    # Pre-populate cache with one stale speaker.
    mgr._speakers["RINCON_OLD"] = _make_fake_speaker("RINCON_OLD")
    fake_soco = MagicMock()
    fake_soco.discover = MagicMock(return_value=None)
    with patch.dict("sys.modules", {"soco": fake_soco}):
        result = await mgr.discover(timeout_s=1)
    assert result == []
    assert mgr.get("RINCON_OLD") is None


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_group_joins_each_member_to_coordinator():
    mgr = SonosManager()
    coord = _make_fake_speaker("RINCON_COORD")
    member_a = _make_fake_speaker("RINCON_A")
    member_b = _make_fake_speaker("RINCON_B")
    mgr._speakers = {
        "RINCON_COORD": coord, "RINCON_A": member_a, "RINCON_B": member_b,
    }
    await mgr.set_group("RINCON_COORD", ["RINCON_COORD", "RINCON_A", "RINCON_B"])
    # Coordinator never joins itself; A + B both joined onto coord.
    coord.join.assert_not_called()
    member_a.join.assert_called_once_with(coord)
    member_b.join.assert_called_once_with(coord)


@pytest.mark.asyncio
async def test_set_group_unknown_coordinator_is_no_op():
    mgr = SonosManager()
    member = _make_fake_speaker("RINCON_A")
    mgr._speakers = {"RINCON_A": member}
    # Coordinator UID isn't in the cache — should log + return without
    # raising or touching member.
    await mgr.set_group("RINCON_UNKNOWN", ["RINCON_A"])
    member.join.assert_not_called()


@pytest.mark.asyncio
async def test_unjoin_detaches_speaker():
    mgr = SonosManager()
    sp = _make_fake_speaker("RINCON_A")
    mgr._speakers = {"RINCON_A": sp}
    await mgr.unjoin("RINCON_A")
    sp.unjoin.assert_called_once()


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_uri_pushes_to_speaker():
    mgr = SonosManager()
    sp = _make_fake_speaker("RINCON_A")
    mgr._speakers = {"RINCON_A": sp}
    await mgr.play_uri("RINCON_A", "http://server:8081/zone/1.flac")
    sp.play_uri.assert_called_once_with(
        "http://server:8081/zone/1.flac", "",
    )


@pytest.mark.asyncio
async def test_set_volume_clamps_0_100():
    mgr = SonosManager()
    sp = _make_fake_speaker("RINCON_A")
    mgr._speakers = {"RINCON_A": sp}
    await mgr.set_volume("RINCON_A", 250)
    assert sp.volume == 100
    await mgr.set_volume("RINCON_A", -10)
    assert sp.volume == 0


# ---------------------------------------------------------------------------
# SonosOutput — direct-URL semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_is_direct_url():
    mgr = MagicMock()
    out = SonosOutput(manager=mgr, speaker_uid="RINCON_A")
    assert out.is_direct_url is True
    # supports_direct_url is True for any track — Sonos pulls from URL.
    fake_track = MagicMock()
    assert out.supports_direct_url(fake_track) is True


@pytest.mark.asyncio
async def test_output_start_pushes_url_to_manager():
    mgr = MagicMock()
    mgr.play_uri = AsyncMock()
    out = SonosOutput(manager=mgr, speaker_uid="RINCON_A")
    stream_info = SimpleNamespace(url="http://server:8081/zone/1.flac")
    await out.start(stream_info=stream_info)
    mgr.play_uri.assert_awaited_once_with(
        "RINCON_A", "http://server:8081/zone/1.flac",
    )


@pytest.mark.asyncio
async def test_output_set_volume_translates_0_to_100():
    mgr = MagicMock()
    mgr.set_volume = AsyncMock()
    out = SonosOutput(manager=mgr, speaker_uid="RINCON_A")
    await out.set_volume(0.5)
    mgr.set_volume.assert_awaited_once_with("RINCON_A", 50)
