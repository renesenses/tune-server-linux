from __future__ import annotations

import asyncio
import platform
import re
import shutil
from pathlib import Path

import structlog

from tune_server.config import settings
from tune_server.db.engine import Database
from tune_server.event_bus import Event, EventBus, EventType
from tune_server.library.scanner import LibraryScanner
from tune_server.models import MountInfo, MountRequest, ShareProtocol

logger = structlog.get_logger()

_IS_MACOS = platform.system() == "Darwin"


class MountManager:
    """Manages mounting/unmounting of network SMB/NFS shares."""

    def __init__(
        self,
        db: Database,
        event_bus: EventBus,
        scanner: LibraryScanner,
        mount_base_dir: str,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._scanner = scanner
        self._mount_base = Path(mount_base_dir).expanduser().resolve()

    async def initialize(self) -> None:
        """Re-mount shares with auto_mount=1 at startup."""
        self._mount_base.mkdir(parents=True, exist_ok=True)

        mounts = await self.list_mounts()
        for mount in mounts:
            if not mount.auto_mount:
                continue

            mount_path = mount.mount_path
            is_os_mounted = Path(mount_path).is_mount()

            if is_os_mounted:
                # OS mount persisted across restart — just add to music_dirs
                if mount_path not in settings.music_dirs:
                    settings.music_dirs.append(mount_path)
                    logger.info("mount_restored", mount_path=mount_path, mount_id=mount.id)
                # Ensure DB status is correct
                if mount.status != "mounted":
                    await self._db.execute(
                        "UPDATE network_mounts SET status = 'mounted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (mount.id,),
                    )
                    await self._db.commit()
            else:
                # Need to (re-)mount
                try:
                    await self._remount(mount.id)
                except Exception:
                    logger.warning("auto_remount_failed", mount_id=mount.id, host=mount.host, share=mount.share_name)

    async def mount_share(self, request: MountRequest) -> MountInfo:
        """Mount a network share, persist in DB, add to music_dirs, and trigger scan."""
        # Build mount point path
        safe_name = re.sub(r"[^\w\-.]", "_", f"{request.host}_{request.share_name}")
        mount_point = self._mount_base / safe_name
        mount_point.mkdir(parents=True, exist_ok=True)

        mount_path = str(mount_point)

        # Persist to DB first
        try:
            cursor = await self._db.execute(
                """INSERT INTO network_mounts (host, share_name, protocol, mount_path, username, password, auto_mount, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'unmounted')""",
                (
                    request.host,
                    request.share_name,
                    request.protocol.value,
                    mount_path,
                    request.username,
                    request.password,
                    1 if request.auto_mount else 0,
                ),
            )
            await self._db.commit()
            mount_id = cursor.lastrowid
        except Exception as e:
            # Unique constraint violation — already exists
            row = await self._db.fetchone(
                "SELECT id FROM network_mounts WHERE host = ? AND share_name = ? AND protocol = ?",
                (request.host, request.share_name, request.protocol.value),
            )
            if row:
                mount_id = row["id"]
                # Update credentials if provided
                await self._db.execute(
                    """UPDATE network_mounts SET username = ?, password = ?, auto_mount = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (request.username, request.password, 1 if request.auto_mount else 0, mount_id),
                )
                await self._db.commit()
            else:
                raise

        # Attempt OS mount
        success, error = await self._os_mount(request, mount_path)

        if success:
            await self._db.execute(
                "UPDATE network_mounts SET status = 'mounted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mount_id,),
            )
            await self._db.commit()

            # Add to music_dirs and scan
            if mount_path not in settings.music_dirs:
                settings.music_dirs.append(mount_path)

            await self._event_bus.emit(Event(
                type=EventType.NETWORK_MOUNT_ADDED,
                data={"id": mount_id, "mount_path": mount_path, "host": request.host, "share": request.share_name},
                source="mount_manager",
            ))

            # Trigger scan in background
            asyncio.create_task(self._scanner.scan([mount_path]))

            return await self._get_mount(mount_id)
        else:
            await self._db.execute(
                "UPDATE network_mounts SET status = 'error', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mount_id,),
            )
            await self._db.commit()

            info = await self._get_mount(mount_id)
            info.error = error
            return info

    async def unmount_share(self, mount_id: int) -> None:
        """Unmount a share, remove from music_dirs, clean up tracks."""
        mount = await self._get_mount(mount_id)
        if not mount:
            return

        # OS unmount
        if mount.status == "mounted":
            await self._os_unmount(mount.mount_path)

        # Remove from music_dirs
        if mount.mount_path in settings.music_dirs:
            settings.music_dirs.remove(mount.mount_path)

        # Delete tracks from DB that are under this mount path
        await self._db.execute(
            "DELETE FROM tracks WHERE file_path LIKE ?",
            (mount.mount_path + "/%",),
        )
        await self._db.commit()

        # Delete mount record
        await self._db.execute("DELETE FROM network_mounts WHERE id = ?", (mount_id,))
        await self._db.commit()

        await self._event_bus.emit(Event(
            type=EventType.NETWORK_MOUNT_REMOVED,
            data={"id": mount_id, "mount_path": mount.mount_path},
            source="mount_manager",
        ))

        # Clean up mount directory
        mount_dir = Path(mount.mount_path)
        if mount_dir.exists() and mount_dir.is_dir():
            try:
                mount_dir.rmdir()  # Only removes if empty
            except OSError:
                pass

    async def remount(self, mount_id: int) -> MountInfo:
        """Re-mount an existing share."""
        return await self._remount(mount_id)

    async def list_mounts(self) -> list[MountInfo]:
        """List all configured mounts."""
        rows = await self._db.fetchall("SELECT * FROM network_mounts ORDER BY id")
        return [self._row_to_mount(r) for r in rows]

    async def _get_mount(self, mount_id: int) -> MountInfo | None:
        row = await self._db.fetchone("SELECT * FROM network_mounts WHERE id = ?", (mount_id,))
        if not row:
            return None
        return self._row_to_mount(row)

    async def _remount(self, mount_id: int) -> MountInfo:
        """Re-mount an existing mount from DB."""
        row = await self._db.fetchone("SELECT * FROM network_mounts WHERE id = ?", (mount_id,))
        if not row:
            raise ValueError(f"Mount {mount_id} not found")

        mount_path = row["mount_path"]
        Path(mount_path).mkdir(parents=True, exist_ok=True)

        request = MountRequest(
            host=row["host"],
            share_name=row["share_name"],
            protocol=ShareProtocol(row["protocol"]),
            username=row["username"],
            password=row["password"],
            auto_mount=bool(row["auto_mount"]),
        )

        success, error = await self._os_mount(request, mount_path)

        if success:
            await self._db.execute(
                "UPDATE network_mounts SET status = 'mounted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mount_id,),
            )
            await self._db.commit()

            if mount_path not in settings.music_dirs:
                settings.music_dirs.append(mount_path)

            return await self._get_mount(mount_id)
        else:
            await self._db.execute(
                "UPDATE network_mounts SET status = 'error', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mount_id,),
            )
            await self._db.commit()

            info = await self._get_mount(mount_id)
            info.error = error
            return info

    async def _os_mount(self, request: MountRequest, mount_path: str) -> tuple[bool, str | None]:
        """Execute OS mount command. Returns (success, error_message)."""
        try:
            cmd = self._build_mount_cmd(request, mount_path)
            # On Linux, mount requires root — use sudo (configured via sudoers)
            if not _IS_MACOS:
                cmd = ["sudo", "-n"] + cmd
            logger.info("os_mount_exec", cmd=cmd, host=request.host, share=request.share_name)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode == 0:
                logger.info("os_mount_success", mount_path=mount_path)
                return True, None
            else:
                error = stderr.decode(errors="replace").strip()
                # Provide helpful message for permission errors
                if "permission" in error.lower() or "operation not permitted" in error.lower():
                    manual_cmd = " ".join(cmd)
                    error = f"{error}. Try manually: sudo {manual_cmd}"
                logger.warning("os_mount_failed", mount_path=mount_path, error=error)
                return False, error

        except asyncio.TimeoutError:
            return False, "Mount command timed out (30s)"
        except FileNotFoundError as e:
            return False, f"Mount command not found: {e.filename}"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _build_mount_cmd(request: MountRequest, mount_path: str) -> list[str]:
        """Build the OS-specific mount command."""
        if request.protocol == ShareProtocol.SMB:
            if _IS_MACOS:
                # mount_smbfs //[user:pass@]host/share /mount/point
                auth = ""
                if request.username:
                    auth = request.username
                    if request.password:
                        auth += f":{request.password}"
                    auth += "@"
                else:
                    auth = "guest@"
                return [
                    "mount_smbfs",
                    f"//{auth}{request.host}/{request.share_name}",
                    mount_path,
                ]
            else:
                # mount.cifs //host/share /mount/point -o username=...,password=...
                cmd = [
                    "mount.cifs",
                    f"//{request.host}/{request.share_name}",
                    mount_path,
                    "-o",
                ]
                opts = []
                if request.username:
                    opts.append(f"username={request.username}")
                    if request.password:
                        opts.append(f"password={request.password}")
                else:
                    opts.append("guest")
                cmd.append(",".join(opts))
                return cmd

        else:  # NFS
            share_path = request.share_name
            if _IS_MACOS:
                return ["mount_nfs", f"{request.host}:{share_path}", mount_path]
            else:
                return ["mount.nfs", f"{request.host}:{share_path}", mount_path]

    async def _os_unmount(self, mount_path: str) -> None:
        """Execute OS unmount command."""
        try:
            cmd = ["umount", mount_path]
            if not _IS_MACOS:
                cmd = ["sudo", "-n"] + cmd
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("os_unmount_success", mount_path=mount_path)
            else:
                logger.warning("os_unmount_failed", mount_path=mount_path)
        except Exception:
            logger.exception("os_unmount_error", mount_path=mount_path)

    @staticmethod
    def _row_to_mount(row) -> MountInfo:
        return MountInfo(
            id=row["id"],
            host=row["host"],
            share_name=row["share_name"],
            protocol=ShareProtocol(row["protocol"]),
            mount_path=row["mount_path"],
            status=row["status"] or "unmounted",
            auto_mount=bool(row["auto_mount"]),
        )

    async def stop(self) -> None:
        """Graceful shutdown — don't unmount, just stop any pending operations."""
        logger.info("mount_manager_stopped")
