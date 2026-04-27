"""Tests for the UpdateChecker — auto-install gating + version comparison."""
from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest

from tune_server.updater import UpdateChecker


def test_is_newer_basic():
    assert UpdateChecker._is_newer("0.7.19", "0.7.18") is True
    assert UpdateChecker._is_newer("0.7.18", "0.7.18") is False
    assert UpdateChecker._is_newer("0.7.18", "0.7.19") is False
    assert UpdateChecker._is_newer("0.8.0", "0.7.99") is True
    assert UpdateChecker._is_newer("1.0.0", "0.99.99") is True


def test_is_newer_garbage_returns_false():
    # Malformed version strings shouldn't crash the check loop.
    assert UpdateChecker._is_newer("not.a.version", "0.7.18") is False
    assert UpdateChecker._is_newer("", "") is False


@pytest.mark.asyncio
async def test_auto_install_skipped_on_windows():
    """On Windows the running .exe is locked; auto-install must no-op."""
    checker = UpdateChecker(event_bus=None)
    checker._latest_version = "0.7.19"
    checker._download_url = "https://example.com/foo.zip"
    checker._asset_name = "tune-server-0.7.19-windows.zip"

    with patch("tune_server.updater.platform.system", return_value="Windows"):
        with patch.object(checker, "download_and_install", new_callable=AsyncMock) as mock_install:
            await checker._auto_install_and_restart()
            mock_install.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_install_runs_on_linux(monkeypatch):
    """On Linux/macOS the auto-install path runs and signals SIGTERM."""
    import os
    import signal as _signal

    checker = UpdateChecker(event_bus=None)
    checker._latest_version = "0.7.19"
    checker._download_url = "https://example.com/foo.tar.gz"
    checker._asset_name = "tune-server-0.7.19-linux.tar.gz"

    sigterm_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("tune_server.updater.platform.system", lambda: "Linux")
    monkeypatch.setattr("tune_server.updater.os.kill",
                        lambda pid, sig: sigterm_calls.append((pid, sig)))

    with patch.object(checker, "download_and_install", new_callable=AsyncMock, return_value=True) as mock_install:
        await checker._auto_install_and_restart()
        mock_install.assert_awaited_once()

    assert sigterm_calls == [(os.getpid(), _signal.SIGTERM)]


@pytest.mark.asyncio
async def test_auto_install_no_restart_on_failure(monkeypatch):
    """If download_and_install returns False, we do NOT signal restart."""
    sigterm_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("tune_server.updater.platform.system", lambda: "Linux")
    monkeypatch.setattr("tune_server.updater.os.kill",
                        lambda pid, sig: sigterm_calls.append((pid, sig)))

    checker = UpdateChecker(event_bus=None)
    checker._latest_version = "0.7.19"
    checker._download_url = "https://example.com/foo.tar.gz"
    checker._asset_name = "tune-server-0.7.19-linux.tar.gz"

    with patch.object(checker, "download_and_install", new_callable=AsyncMock, return_value=False):
        await checker._auto_install_and_restart()

    assert sigterm_calls == [], "Failed install must NOT trigger SIGTERM"
