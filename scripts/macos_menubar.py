"""macOS menubar wrapper for Tune Server.

Runs tune-server as a background subprocess and exposes a status menu in
the menu bar. No dock icon (LSUIElement=true). Logs go to
~/Library/Logs/Tune Server.log.
"""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import rumps


def _runtime_binary() -> Path:
    """Locate the tune-server binary inside the .app bundle.

    Bundle layout:
      Tune Server.app/Contents/MacOS/Tune Server  (this wrapper)
      Tune Server.app/Contents/Resources/runtime/tune-server
    """
    here = Path(sys.executable).resolve().parent  # Contents/MacOS
    return here.parent / "Resources" / "runtime" / "tune-server"


def _read_bundled_version() -> str:
    """Pull the runtime's version from its PyInstaller dist-info.

    The wrapper used to fall back to a hardcoded '0.7.38' when no
    TUNE_VERSION env var was set, so every menubar always claimed
    0.7.38 regardless of what was actually shipping. Read it from the
    runtime's _internal/tune_server-X.Y.Z.dist-info/ folder name
    instead — that's regenerated every PyInstaller build, so it's
    always co-temporal with the binary it labels.
    """
    runtime_dir = _runtime_binary().parent
    internal = runtime_dir / "_internal"
    if internal.is_dir():
        for entry in internal.iterdir():
            name = entry.name
            if name.startswith("tune_server-") and name.endswith(".dist-info"):
                return name[len("tune_server-"):-len(".dist-info")]
    return os.environ.get("TUNE_VERSION", "unknown")


VERSION = _read_bundled_version()


def _log_path() -> Path:
    p = Path.home() / "Library" / "Logs" / "Tune Server.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rotate_log(path: Path, max_bytes: int = 5 * 1024 * 1024, keep: int = 2) -> None:
    """Rotate log if it exceeds max_bytes. Keep up to `keep` old copies."""
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for i in range(keep - 1, 0, -1):
        src = path.with_suffix(f".log.{i}")
        dst = path.with_suffix(f".log.{i + 1}")
        if src.exists():
            src.rename(dst)
    path.rename(path.with_suffix(".log.1"))


class TuneServerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("🎵", quit_button=None)
        self.server_proc: subprocess.Popen | None = None
        _rotate_log(_log_path())
        self.log_file = open(_log_path(), "a", buffering=1)

        self.status_item = rumps.MenuItem("Status: démarrage…")
        self.version_item = rumps.MenuItem(f"Tune Server v{VERSION}")
        # Update banner — visible/hidden depending on /system/update/check.
        # Inserted between status and "Ouvrir l'interface web" so it sits
        # at the top where users will notice it without scrolling. Hidden
        # by default; populated by _refresh_update_status.
        self.update_item = rumps.MenuItem(
            "Mise à jour disponible…",
            callback=self.install_update,
        )
        self.update_item.hidden = True
        self._latest_version: str | None = None
        # Native SwiftUI client — only meaningful when /Applications/Tune.app
        # is also installed (the case for combo .pkg installs). For
        # DMG-only installs (server alone) the entry would be a dead end,
        # so we hide it and tell users it's a no-op.
        self._tune_app_path = Path("/Applications/Tune.app")
        menu_items: list = [
            self.version_item,
            self.status_item,
            self.update_item,
            None,
            rumps.MenuItem("Ouvrir l'interface web", callback=self.open_web_ui),
        ]
        if self._tune_app_path.is_dir():
            menu_items.append(
                rumps.MenuItem("Ouvrir Tune (app native)", callback=self.open_tune_app)
            )
        menu_items += [
            rumps.MenuItem("Voir les logs", callback=self.show_logs),
            None,
            rumps.MenuItem("Redémarrer le serveur", callback=self.restart_server),
            None,
            rumps.MenuItem("Quitter", callback=self.quit_app),
        ]
        self.menu = menu_items
        self.version_item.set_callback(None)  # static
        self.status_item.set_callback(None)

        self._start_server()

        # First update check after 45s — gives the bundled server enough
        # time to boot, hit GitHub, and cache the result before we poll
        # /update/check. Without this we'd wait the full 30 min before
        # the badge appears on a fresh launch when an update is pending.
        self._initial_check_timer = rumps.Timer(self._initial_update_check, 45)
        self._initial_check_timer.start()

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    def _start_server(self) -> None:
        bin_path = _runtime_binary()
        if not bin_path.exists():
            rumps.alert(
                title="Tune Server",
                message=f"Binaire introuvable :\n{bin_path}",
            )
            return

        # Server writes its DB + caches in cwd. /Applications is read-only,
        # so use a per-user data directory under Application Support.
        data_dir = Path.home() / "Library" / "Application Support" / "Tune Server"
        data_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["TUNE_VERSION"] = VERSION
        # Keep TUNE_DB_PATH in sync with cwd so the server's default DB lookup
        # also lands in the writable directory.
        env.setdefault("TUNE_DB_PATH", str(data_dir / "tune_server.db"))
        env.setdefault("TUNE_ARTWORK_CACHE_DIR", str(data_dir / "artwork_cache"))

        self.log_file.write(
            f"\n=== Tune Server v{VERSION} starting at {datetime.now().isoformat()} (data: {data_dir}) ===\n"
        )
        self.log_file.flush()
        try:
            self.server_proc = subprocess.Popen(
                [str(bin_path)],
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                cwd=str(data_dir),
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            self.log_file.write(f"failed to start: {exc!r}\n")
            self.log_file.flush()
            self.status_item.title = "Status: échec démarrage"
            return
        self.status_item.title = "Status: en cours d'exécution ●"

    def _stop_server(self, timeout: float = 5.0) -> None:
        if self.server_proc is None:
            return
        if self.server_proc.poll() is not None:
            return
        try:
            self.server_proc.terminate()
            self.server_proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.server_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        except Exception:
            pass

    # ─── Periodic status refresh ────────────────────────────────────────────

    @rumps.timer(3)
    def _refresh_status(self, _sender) -> None:
        if self.server_proc is None:
            self.status_item.title = "Status: arrêté"
            return
        if self.server_proc.poll() is None:
            self.status_item.title = "Status: en cours d'exécution ●"
        else:
            code = self.server_proc.returncode
            self.status_item.title = f"Status: arrêté (code {code})"

    # Poll the running server's /update/check every 30 min. Mirrors what
    # the web client does — same banner, same source of truth. The icon
    # gets a 🔴 prefix when an update is available so users see it
    # without opening the menu.
    def _initial_update_check(self, _sender) -> None:
        # One-shot — stop the timer so it doesn't keep firing.
        self._initial_check_timer.stop()
        self._refresh_update_status(_sender)

    @rumps.timer(1800)
    def _refresh_update_status(self, _sender) -> None:
        if self.server_proc is None or self.server_proc.poll() is not None:
            return
        try:
            req = urllib.request.Request(
                "http://localhost:8888/api/v1/system/update/check",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return
        if data.get("update_available") and data.get("latest_version"):
            self._latest_version = data["latest_version"]
            self.update_item.title = (
                f"⬆ Mise à jour disponible : v{self._latest_version}"
            )
            self.update_item.hidden = False
            self.title = "🔴 🎵"
        else:
            self._latest_version = None
            self.update_item.hidden = True
            self.title = "🎵"

    # ─── Menu actions ───────────────────────────────────────────────────────

    def open_web_ui(self, _sender) -> None:
        webbrowser.open("http://localhost:8888")

    def open_tune_app(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", str(self._tune_app_path)])

    def install_update(self, _sender) -> None:
        if not self._latest_version:
            return
        resp = rumps.alert(
            title="Mise à jour disponible",
            message=f"Tune Server v{self._latest_version} est disponible.\n\n"
                    "Le DMG va se télécharger automatiquement.\n"
                    "Il s'ouvrira ensuite — glissez simplement la nouvelle app dans Applications.",
            ok="Mettre à jour",
            cancel="Plus tard",
        )
        if resp != 1:
            return

        # Determine the correct DMG filename for this architecture.
        machine = platform.machine().lower()
        if "arm" in machine or "aarch64" in machine:
            dmg_name = f"tune-server-{self._latest_version}-macos.dmg"
        else:
            dmg_name = f"tune-server-{self._latest_version}-macos-intel.dmg"

        dmg_url = (
            f"https://github.com/renesenses/tune-server-linux/releases/"
            f"download/v{self._latest_version}/{dmg_name}"
        )
        dmg_dest = Path.home() / "Downloads" / dmg_name

        # Disable the menu item during download to prevent double-clicks.
        self.update_item.set_callback(None)
        self.update_item.title = "Téléchargement en cours... 0%"

        # Download in a background thread so the menubar stays responsive.
        thread = threading.Thread(
            target=self._download_and_open_dmg,
            args=(dmg_url, dmg_dest),
            daemon=True,
        )
        thread.start()

    def _download_and_open_dmg(self, url: str, dest: Path) -> None:
        """Download the DMG, stop the server, open the DMG, quit the app.

        Runs on a background thread. Uses rumps.Timer one-shot callbacks
        to touch the UI from the main thread (rumps is not thread-safe).
        """
        try:
            self.log_file.write(
                f"[{datetime.now().isoformat()}] Downloading update: {url}\n"
            )
            self.log_file.flush()

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=300) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 64 * 1024  # 64 KB
                last_pct = -1

                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = int(downloaded * 100 / total)
                            # Update progress at every 5% to avoid flooding.
                            if pct >= last_pct + 5:
                                last_pct = pct
                                self.update_item.title = (
                                    f"Téléchargement en cours... {pct}%"
                                )

            self.log_file.write(
                f"[{datetime.now().isoformat()}] Download complete: {dest} "
                f"({dest.stat().st_size} bytes)\n"
            )
            self.log_file.flush()

            self.update_item.title = "Téléchargement terminé, installation..."

            # Stop the server before opening the DMG so the user can
            # drag-install without "app is in use" errors.
            self._stop_server()
            self.status_item.title = "Status: arrêté pour mise à jour"

            # Show a notification so the user knows the DMG is ready.
            rumps.notification(
                title="Tune Server",
                subtitle=f"v{self._latest_version} téléchargé",
                message="Glissez la nouvelle app dans Applications.",
            )

            # Open the DMG — Finder will mount it and show the drag-install
            # window with the branded background.
            subprocess.Popen(["/usr/bin/open", str(dest)])

            self.log_file.write(
                f"[{datetime.now().isoformat()}] DMG opened, quitting menubar app\n"
            )
            self.log_file.flush()

            # Quit the menubar app so the user can cleanly drag-install.
            # Small delay so the notification has time to appear.
            import time
            time.sleep(2)
            rumps.quit_application()

        except Exception as exc:
            self.log_file.write(
                f"[{datetime.now().isoformat()}] Update download failed: {exc!r}\n"
            )
            self.log_file.flush()

            self.update_item.title = "Échec du téléchargement"
            # Re-enable the callback so the user can retry.
            self.update_item.set_callback(self.install_update)

            # Fallback: open the release page in the browser.
            rumps.notification(
                title="Tune Server",
                subtitle="Échec du téléchargement",
                message="Ouverture de la page de téléchargement...",
            )
            webbrowser.open(
                f"https://github.com/renesenses/tune-server-linux/releases/"
                f"tag/v{self._latest_version}"
            )

    def show_logs(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", "-a", "Console", str(_log_path())])

    def restart_server(self, _sender) -> None:
        self.status_item.title = "Status: redémarrage…"
        self._stop_server()
        self._start_server()

    def quit_app(self, _sender) -> None:
        self._stop_server()
        # Clean sentinel so next launch tries rumps again
        sentinel = Path.home() / "Library" / "Application Support" / "Tune Server" / ".rumps_failed"
        sentinel.unlink(missing_ok=True)
        try:
            self.log_file.close()
        except Exception:
            pass
        rumps.quit_application()


def _fallback_run() -> None:
    """Fallback when rumps fails: run tune-server directly + open browser."""
    import time
    bin_path = _runtime_binary()
    if not bin_path.exists():
        print(f"[Tune Server] Binary not found: {bin_path}")
        return

    data_dir = Path.home() / "Library" / "Application Support" / "Tune Server"
    data_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["TUNE_VERSION"] = VERSION
    env.setdefault("TUNE_DB_PATH", str(data_dir / "tune_server.db"))
    env.setdefault("TUNE_ARTWORK_CACHE_DIR", str(data_dir / "artwork_cache"))

    print(f"[Tune Server] Starting v{VERSION} (fallback mode — no menubar icon)")
    print(f"[Tune Server] Data: {data_dir}")
    print(f"[Tune Server] Web UI: http://localhost:8888")

    proc = subprocess.Popen(
        [str(bin_path)],
        cwd=str(data_dir),
        env=env,
    )

    time.sleep(3)
    webbrowser.open("http://localhost:8888")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=5)


def main() -> None:
    # Skip rumps entirely if a previous launch hung (sentinel file).
    # Also skip if --no-menubar is passed explicitly.
    sentinel = Path.home() / "Library" / "Application Support" / "Tune Server" / ".rumps_failed"
    if sentinel.exists() or "--no-menubar" in sys.argv:
        sentinel.unlink(missing_ok=True)
        _fallback_run()
        return

    # Write sentinel before attempting rumps — if the app hangs and is
    # force-killed, the sentinel survives and the next launch skips rumps.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("pending")

    try:
        app = TuneServerApp()
        # If we get past __init__ without hanging, rumps is likely OK.
        # Remove sentinel — the quit handler also removes it on clean exit.
        sentinel.unlink(missing_ok=True)
        app.run()
    except Exception as exc:
        sentinel.unlink(missing_ok=True)
        print(f"[Tune Server] Menubar failed: {exc}")
        print("[Tune Server] Falling back to direct server mode...")
        _fallback_run()


if __name__ == "__main__":
    main()
