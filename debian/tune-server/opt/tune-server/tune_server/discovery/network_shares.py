from __future__ import annotations

import asyncio
import platform
import subprocess

import structlog

from tune_server.event_bus import Event, EventBus, EventType
from tune_server.models import NetworkShare, ShareProtocol

logger = structlog.get_logger()

_IS_MACOS = platform.system() == "Darwin"

SMB_SERVICE = "_smb._tcp.local."
NFS_SERVICE = "_nfs._tcp.local."


class NetworkShareDiscovery:
    """mDNS/Zeroconf discovery of SMB and NFS shares on the local network."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._shares: dict[str, NetworkShare] = {}
        self._zeroconf = None
        self._browsers: list = []
        self._running = False
        self._lock = asyncio.Lock()
        self._enum_tasks: dict[str, asyncio.Task] = {}

    @property
    def shares(self) -> dict[str, NetworkShare]:
        return dict(self._shares)

    async def start(self) -> None:
        self._running = True
        try:
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

            self._zeroconf = AsyncZeroconf()

            listener = _ShareListener(self)
            self._browsers = [
                AsyncServiceBrowser(
                    self._zeroconf.zeroconf,
                    [SMB_SERVICE, NFS_SERVICE],
                    handlers=[listener.on_state_change],
                ),
            ]
            logger.info("network_share_discovery_started")
        except ImportError:
            logger.warning("zeroconf_not_installed", hint="pip install zeroconf")
        except Exception:
            logger.exception("network_share_discovery_start_error")

    async def stop(self) -> None:
        self._running = False

        # Cancel enumeration tasks
        for task in self._enum_tasks.values():
            task.cancel()
        for task in self._enum_tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._enum_tasks.clear()

        # Close browsers
        for browser in self._browsers:
            try:
                browser.cancel()
            except Exception:
                pass
        self._browsers.clear()

        if self._zeroconf:
            try:
                await self._zeroconf.async_close()
            except Exception:
                pass
            self._zeroconf = None

    async def on_service_added(self, service_type: str, name: str) -> None:
        """Called by listener when a new SMB/NFS service is discovered."""
        if not self._running or not self._zeroconf:
            return

        try:
            from zeroconf import Zeroconf

            info = await self._zeroconf.async_get_service_info(service_type, name)
            if not info:
                return

            host = None
            if info.parsed_addresses():
                host = info.parsed_addresses()[0]
            if not host:
                host = info.server or ""
            host = host.rstrip(".")

            port = info.port or 0
            display_name = name.replace(f".{service_type}", "").strip()

            if service_type == SMB_SERVICE:
                protocol = ShareProtocol.SMB
                share_id = f"smb://{host}"
            else:
                protocol = ShareProtocol.NFS
                share_id = f"nfs://{host}"

            share = NetworkShare(
                id=share_id,
                name=display_name,
                host=host,
                port=port,
                protocol=protocol,
            )

            async with self._lock:
                is_new = share_id not in self._shares
                was_lost = share_id in self._shares and not self._shares[share_id].available
                self._shares[share_id] = share

            if is_new or was_lost:
                await self._event_bus.emit(Event(
                    type=EventType.NETWORK_SHARE_DISCOVERED,
                    data=share.model_dump(),
                    source="network_shares",
                ))
                logger.info("network_share_found", name=display_name, host=host, protocol=protocol.value)

                # Enumerate shares in background
                if share_id not in self._enum_tasks or self._enum_tasks[share_id].done():
                    self._enum_tasks[share_id] = asyncio.create_task(
                        self._enumerate_shares(share_id, host, protocol)
                    )

        except Exception:
            logger.exception("network_share_add_error", name=name)

    async def on_service_removed(self, service_type: str, name: str) -> None:
        """Called by listener when a SMB/NFS service disappears."""
        # Derive the share_id from the service name
        display_name = name.replace(f".{service_type}", "").strip()

        async with self._lock:
            for share_id, share in self._shares.items():
                if share.name == display_name and share.available:
                    share.available = False
                    await self._event_bus.emit(Event(
                        type=EventType.NETWORK_SHARE_LOST,
                        data={"id": share_id, "name": share.name},
                        source="network_shares",
                    ))
                    logger.info("network_share_lost", name=display_name, id=share_id)
                    break

    async def _enumerate_shares(self, share_id: str, host: str, protocol: ShareProtocol) -> None:
        """Enumerate available shares/exports on a host via smbclient or showmount."""
        try:
            if protocol == ShareProtocol.SMB:
                shares = await self._list_smb_shares(host)
            else:
                shares = await self._list_nfs_exports(host)

            async with self._lock:
                if share_id in self._shares:
                    self._shares[share_id].shares = shares
                    logger.info("network_shares_enumerated", host=host, protocol=protocol.value, shares=shares)

        except Exception:
            logger.debug("share_enumeration_error", host=host, protocol=protocol.value)

    @staticmethod
    async def _list_smb_shares(host: str) -> list[str]:
        """List SMB shares on a host using smbutil (macOS) or smbclient (Linux)."""
        if _IS_MACOS:
            return await NetworkShareDiscovery._list_smb_shares_smbutil(host)
        return await NetworkShareDiscovery._list_smb_shares_smbclient(host)

    @staticmethod
    async def _list_smb_shares_smbutil(host: str) -> list[str]:
        """List SMB shares using macOS smbutil view."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "smbutil", "view", f"//guest@{host}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")

            shares = []
            in_share_section = False
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("Share") and "Type" in stripped:
                    in_share_section = True
                    continue
                if stripped.startswith("---"):
                    continue
                if in_share_section:
                    if not stripped:
                        continue
                    # "shares listed" summary line
                    if "listed" in stripped:
                        break
                    parts = stripped.split()
                    if len(parts) >= 2:
                        name = parts[0]
                        stype = parts[1]
                        # Skip IPC$ and Pipe types
                        if stype == "Disk" and not name.endswith("$"):
                            shares.append(name)
            return shares

        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    @staticmethod
    async def _list_smb_shares_smbclient(host: str) -> list[str]:
        """List SMB shares using smbclient -N -L (Linux)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "smbclient", "-N", "-L", f"//{host}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")

            shares = []
            in_share_section = False
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("Sharename") or stripped.startswith("---------"):
                    in_share_section = True
                    continue
                if in_share_section:
                    if not stripped or stripped.startswith("Server") or stripped.startswith("Workgroup"):
                        in_share_section = False
                        continue
                    parts = stripped.split()
                    if len(parts) >= 2:
                        name = parts[0]
                        stype = parts[1]
                        # Skip printers and IPC
                        if stype == "Disk":
                            shares.append(name)
            return shares

        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    @staticmethod
    async def _list_nfs_exports(host: str) -> list[str]:
        """List NFS exports on a host using showmount -e."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "showmount", "-e", host,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")

            exports = []
            for line in output.splitlines()[1:]:  # Skip header
                stripped = line.strip()
                if stripped:
                    # Format: /export/path  host_list
                    parts = stripped.split()
                    if parts:
                        exports.append(parts[0])
            return exports

        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    async def enumerate_host_shares(self, share_id: str) -> list[str]:
        """Manually trigger share enumeration for a host and return the result."""
        async with self._lock:
            share = self._shares.get(share_id)
        if not share:
            return []

        await self._enumerate_shares(share_id, share.host, share.protocol)

        async with self._lock:
            share = self._shares.get(share_id)
        return share.shares if share else []


class _ShareListener:
    """Zeroconf ServiceListener adapter."""

    def __init__(self, discovery: NetworkShareDiscovery) -> None:
        self._discovery = discovery

    def on_state_change(self, zeroconf, service_type: str, name: str, state_change) -> None:
        from zeroconf import ServiceStateChange

        if state_change == ServiceStateChange.Added:
            asyncio.ensure_future(self._discovery.on_service_added(service_type, name))
        elif state_change == ServiceStateChange.Removed:
            asyncio.ensure_future(self._discovery.on_service_removed(service_type, name))
