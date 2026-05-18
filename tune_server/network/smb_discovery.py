"""Periodic SMB share auto-discovery.

Complements the mDNS-based ``NetworkShareDiscovery`` by running active
scans every ``scan_interval`` seconds.  On Linux it shells out to
``smbclient``; on macOS to ``smbutil``; on Windows to ``net view``.

Discovered hosts and their exported shares are cached and exposed via
the ``discovered`` property so the API can list them.
"""

from __future__ import annotations

import asyncio
import platform
import re
import subprocess
import time
from dataclasses import dataclass, field

import structlog

from tune_server.event_bus import Event, EventBus, EventType

logger = structlog.get_logger()

_IS_MACOS = platform.system() == "Darwin"
_IS_WINDOWS = platform.system() == "Windows"

# Default scan interval in seconds
DEFAULT_SCAN_INTERVAL = 60


@dataclass
class DiscoveredSmbShare:
    """A single discovered SMB share on the network."""

    host: str
    share_name: str
    host_name: str = ""
    last_seen: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return f"smb://{self.host}/{self.share_name}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "host_name": self.host_name,
            "share_name": self.share_name,
            "last_seen": self.last_seen,
        }


class SmbAutoDiscovery:
    """Periodically scans the local network for SMB shares.

    Uses subnet-based host scanning (finds hosts via ARP table / ping
    sweep) then enumerates shares on each host.
    """

    def __init__(
        self,
        event_bus: EventBus,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self._event_bus = event_bus
        self._scan_interval = scan_interval
        self._discovered: dict[str, DiscoveredSmbShare] = {}
        self._known_hosts: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def discovered(self) -> dict[str, DiscoveredSmbShare]:
        """Return a snapshot of all currently discovered shares."""
        return dict(self._discovered)

    async def start(self) -> None:
        """Start the periodic scan loop."""
        self._running = True
        # Run an immediate scan then schedule periodic ones
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("smb_auto_discovery_started", interval=self._scan_interval)

    async def stop(self) -> None:
        """Stop the scan loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("smb_auto_discovery_stopped")

    async def scan_now(self) -> list[DiscoveredSmbShare]:
        """Trigger an immediate scan and return results."""
        await self._do_scan()
        return list(self._discovered.values())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """Periodic background scan."""
        while self._running:
            try:
                await self._do_scan()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("smb_scan_error")
            try:
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                break

    async def _do_scan(self) -> None:
        """Run a single discovery pass."""
        hosts = await self._find_smb_hosts()
        new_shares: list[DiscoveredSmbShare] = []

        for host, host_name in hosts:
            try:
                shares = await self._enumerate_shares(host)
                for share_name in shares:
                    share = DiscoveredSmbShare(
                        host=host,
                        share_name=share_name,
                        host_name=host_name,
                        last_seen=time.time(),
                    )
                    async with self._lock:
                        is_new = share.id not in self._discovered
                        self._discovered[share.id] = share
                    if is_new:
                        new_shares.append(share)
            except Exception:
                logger.debug("smb_host_enum_error", host=host)

        if new_shares:
            await self._event_bus.emit(Event(
                type=EventType.SMB_SHARES_DISCOVERED,
                data={"shares": [s.to_dict() for s in new_shares]},
                source="smb_discovery",
            ))
            logger.info("smb_new_shares_found", count=len(new_shares))

    async def _find_smb_hosts(self) -> list[tuple[str, str]]:
        """Find hosts that advertise SMB on the local network.

        Returns list of ``(ip, hostname)`` tuples.
        """
        hosts: list[tuple[str, str]] = []

        # Also include hosts previously discovered via mDNS (already in
        # _known_hosts set) — always re-scan them.
        for host in list(self._known_hosts):
            hosts.append((host, ""))

        if _IS_WINDOWS:
            hosts.extend(await self._find_hosts_netview())
        else:
            hosts.extend(await self._find_hosts_arp())

        # Deduplicate by IP
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for ip, name in hosts:
            if ip not in seen:
                seen.add(ip)
                unique.append((ip, name))
        return unique

    @staticmethod
    async def _find_hosts_arp() -> list[tuple[str, str]]:
        """Parse the ARP table to find hosts (Linux/macOS)."""
        hosts: list[tuple[str, str]] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "arp", "-a",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")
            # ARP output varies:
            # macOS: hostname (ip) at mac on iface ...
            # Linux: hostname (ip) at mac [ether] on iface
            for line in output.splitlines():
                m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
                if m:
                    ip = m.group(1)
                    # Skip broadcast/multicast
                    if ip.endswith(".255") or ip.startswith("224."):
                        continue
                    name_m = re.match(r"^(\S+)\s+\(", line)
                    name = name_m.group(1) if name_m and name_m.group(1) != "?" else ""
                    hosts.append((ip, name))
        except (FileNotFoundError, asyncio.TimeoutError):
            pass
        return hosts

    @staticmethod
    async def _find_hosts_netview() -> list[tuple[str, str]]:
        """Use ``net view`` to find Windows SMB hosts."""
        hosts: list[tuple[str, str]] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "net", "view",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode(errors="replace")
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("\\\\"):
                    parts = stripped.split()
                    host = parts[0].strip("\\")
                    hosts.append((host, host))
        except (FileNotFoundError, asyncio.TimeoutError):
            pass
        return hosts

    @staticmethod
    async def _enumerate_shares(host: str) -> list[str]:
        """Enumerate SMB shares on a single host (platform-adaptive)."""
        if _IS_MACOS:
            return await SmbAutoDiscovery._enum_smbutil(host)
        if _IS_WINDOWS:
            return await SmbAutoDiscovery._enum_netview(host)
        return await SmbAutoDiscovery._enum_smbclient(host)

    @staticmethod
    async def _enum_smbclient(host: str) -> list[str]:
        """List shares via ``smbclient -N -L //host`` (Linux)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "smbclient", "-N", "-L", f"//{host}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")
            return _parse_smbclient_output(output)
        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    @staticmethod
    async def _enum_smbutil(host: str) -> list[str]:
        """List shares via ``smbutil view //guest@host`` (macOS)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "smbutil", "view", f"//guest@{host}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")
            return _parse_smbutil_output(output)
        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    @staticmethod
    async def _enum_netview(host: str) -> list[str]:
        """List shares via ``net view \\\\host`` (Windows)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "net", "view", f"\\\\{host}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode(errors="replace")
            return _parse_netview_output(output)
        except (FileNotFoundError, asyncio.TimeoutError):
            return []

    def add_known_host(self, host: str) -> None:
        """Register a host IP so it is always included in future scans."""
        self._known_hosts.add(host)


# ------------------------------------------------------------------
# Output parsers (shared helpers)
# ------------------------------------------------------------------

def _parse_smbclient_output(output: str) -> list[str]:
    shares: list[str] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sharename") or stripped.startswith("---------"):
            in_section = True
            continue
        if in_section:
            if not stripped or stripped.startswith("Server") or stripped.startswith("Workgroup"):
                in_section = False
                continue
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == "Disk":
                name = parts[0]
                if not name.endswith("$"):
                    shares.append(name)
    return shares


def _parse_smbutil_output(output: str) -> list[str]:
    shares: list[str] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Share") and "Type" in stripped:
            in_section = True
            continue
        if stripped.startswith("---"):
            continue
        if in_section:
            if not stripped or "listed" in stripped:
                break
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == "Disk" and not parts[0].endswith("$"):
                shares.append(parts[0])
    return shares


def _parse_netview_output(output: str) -> list[str]:
    shares: list[str] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("---"):
            in_section = True
            continue
        if in_section:
            if not stripped or stripped.startswith("The command"):
                break
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == "Disk" and not parts[0].endswith("$"):
                shares.append(parts[0])
    return shares
