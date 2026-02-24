from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tune_server.event_bus import EventBus
from tune_server.models import DiscoveredDevice, OutputType


@pytest.fixture
def mock_ssdp():
    ssdp = AsyncMock()
    ssdp.devices = {}
    return ssdp


@pytest.fixture
def mock_mdns():
    mdns = AsyncMock()
    mdns.devices = {}
    return mdns


def _make_device(device_id, name, dtype=OutputType.DLNA):
    return DiscoveredDevice(
        id=device_id,
        name=name,
        type=dtype,
        host="192.168.1.100",
        port=8080,
    )


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
def test_list_devices_empty(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True
    mock_ssdp_cls.return_value = MagicMock(devices={})
    mock_mdns_cls.return_value = MagicMock(devices={})

    dm = DiscoveryManager(event_bus)
    assert dm.list_devices() == []


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
def test_list_devices_combines_ssdp_mdns(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True

    dev_ssdp = _make_device("ssdp-1", "SSDP Speaker")
    dev_mdns = _make_device("mdns-1", "mDNS Speaker")

    mock_ssdp_cls.return_value = MagicMock(devices={"ssdp-1": dev_ssdp})
    mock_mdns_cls.return_value = MagicMock(devices={"mdns-1": dev_mdns})

    dm = DiscoveryManager(event_bus)
    devices = dm.list_devices()
    assert len(devices) == 2


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
def test_get_device_from_ssdp(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True

    dev = _make_device("ssdp-1", "Speaker")
    mock_ssdp_cls.return_value = MagicMock(devices={"ssdp-1": dev})
    mock_mdns_cls.return_value = MagicMock(devices={})

    dm = DiscoveryManager(event_bus)
    found = dm.get_device("ssdp-1")
    assert found is not None
    assert found.name == "Speaker"


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
def test_get_device_from_mdns(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True

    dev = _make_device("mdns-1", "AirPlay Speaker")
    mock_ssdp_cls.return_value = MagicMock(devices={})
    mock_mdns_cls.return_value = MagicMock(devices={"mdns-1": dev})

    dm = DiscoveryManager(event_bus)
    found = dm.get_device("mdns-1")
    assert found is not None
    assert found.name == "AirPlay Speaker"


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
def test_get_device_not_found(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True
    mock_ssdp_cls.return_value = MagicMock(devices={})
    mock_mdns_cls.return_value = MagicMock(devices={})

    dm = DiscoveryManager(event_bus)
    assert dm.get_device("nonexistent") is None


@patch("tune_server.discovery.manager.settings")
@patch("tune_server.discovery.manager.SsdpDiscovery")
@patch("tune_server.discovery.manager.MdnsDiscovery")
async def test_start_stop(mock_mdns_cls, mock_ssdp_cls, mock_settings, event_bus):
    from tune_server.discovery.manager import DiscoveryManager

    mock_settings.ssdp_enabled = True
    mock_settings.mdns_enabled = True
    mock_settings.discovery_enabled = True

    mock_ssdp = AsyncMock()
    mock_mdns = AsyncMock()
    mock_ssdp_cls.return_value = mock_ssdp
    mock_mdns_cls.return_value = mock_mdns

    dm = DiscoveryManager(event_bus)
    await dm.start()
    mock_ssdp.start.assert_awaited_once()
    mock_mdns.start.assert_awaited_once()

    await dm.stop()
    mock_ssdp.stop.assert_awaited_once()
    mock_mdns.stop.assert_awaited_once()
