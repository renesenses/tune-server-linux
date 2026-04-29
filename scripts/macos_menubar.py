"""macOS menubar wrapper for Tune Server.

Runs tune-server as a background subprocess and exposes a status menu in
the menu bar. No dock icon (LSUIElement=true). Logs go to
~/Library/Logs/Tune Server.log.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
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


class TuneServerApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("🎵", quit_button=None)
        self.server_proc: subprocess.Popen | None = None
        self.log_file = open(_log_path(), "a", buffering=1)

        self.status_item = rumps.MenuItem("Status: démarrage…")
        self.version_item = rumps.MenuItem(f"Tune Server v{VERSION}")
        # Native SwiftUI client — only meaningful when /Applications/Tune.app
        # is also installed (the case for combo .pkg installs). For
        # DMG-only installs (server alone) the entry would be a dead end,
        # so we hide it and tell users it's a no-op.
        self._tune_app_path = Path("/Applications/Tune.app")
        menu_items: list = [
            self.version_item,
            self.status_item,
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

    # ─── Menu actions ───────────────────────────────────────────────────────

    def open_web_ui(self, _sender) -> None:
        webbrowser.open("http://localhost:8888")

    def open_tune_app(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", str(self._tune_app_path)])

    def show_logs(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", "-a", "Console", str(_log_path())])

    def restart_server(self, _sender) -> None:
        self.status_item.title = "Status: redémarrage…"
        self._stop_server()
        self._start_server()

    def quit_app(self, _sender) -> None:
        self._stop_server()
        try:
            self.log_file.close()
        except Exception:
            pass
        rumps.quit_application()


def main() -> None:
    TuneServerApp().run()


if __name__ == "__main__":
    main()
