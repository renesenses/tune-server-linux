"""Bug report generator for mozaiklabs.fr forum.

Collects system info, library stats, zone config, recent errors, and
formats them as a markdown template ready to paste into a forum post.
Also provides an HTML page with a copy-to-clipboard button.
"""
from __future__ import annotations

import os
import platform
import re
import sys
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

import structlog

from tune_server.api.deps import deps
from tune_server.config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/system", tags=["system"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(
    r"(/(?:home|Users|mnt|media)/)[^/\s]+",
    re.IGNORECASE,
)


def _sanitize_path(text: str) -> str:
    """Replace usernames in filesystem paths with <user>."""
    return _USERNAME_RE.sub(r"\1<user>", text)


def _sanitize_errors(errors: list[dict]) -> list[dict]:
    """Sanitize a list of error dicts, removing usernames from any values."""
    sanitized: list[dict] = []
    for entry in errors:
        clean: dict[str, Any] = {}
        for k, v in entry.items():
            clean[k] = _sanitize_path(str(v)) if isinstance(v, str) else v
        sanitized.append(clean)
    return sanitized


async def _collect_bug_report_data() -> dict[str, Any]:
    """Collect all data for the bug report. Returns a structured dict."""
    from tune_server import __version__
    from tune_server.utils.error_buffer import recent_errors

    data: dict[str, Any] = {}

    # ── System info ──────────────────────────────────────────────────
    data["version"] = __version__
    data["os"] = platform.platform()
    data["python"] = sys.version.split()[0]
    data["architecture"] = platform.machine()
    data["db_engine"] = getattr(settings, "db_engine", "sqlite")

    # ── FFmpeg ───────────────────────────────────────────────────────
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            data["ffmpeg_version"] = result.stdout.splitlines()[0]
        else:
            data["ffmpeg_version"] = None
    except Exception:
        data["ffmpeg_version"] = None

    # ── Audio devices ────────────────────────────────────────────────
    try:
        import sounddevice as sd
        devices = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                devices.append({
                    "id": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "sample_rate": int(d.get("default_samplerate", 0)),
                })
        data["audio_devices"] = devices
    except Exception:
        data["audio_devices"] = None

    # ── Connected streaming services (names only) ────────────────────
    services_list: list[str] = []
    for name, svc in deps.streaming_services.items():
        try:
            if svc.is_authenticated:
                services_list.append(name)
        except Exception:
            pass
    data["streaming_services"] = services_list

    # ── Music dirs (sanitized) ───────────────────────────────────────
    data["music_dirs"] = [_sanitize_path(d) for d in settings.music_dirs]

    # ── Library stats ────────────────────────────────────────────────
    data["tracks"] = await deps.track_repo.count() if deps.track_repo else 0
    data["albums"] = await deps.album_repo.count() if deps.album_repo else 0
    data["artists"] = await deps.artist_repo.count() if deps.artist_repo else 0

    # ── Zones ────────────────────────────────────────────────────────
    zones = deps.zone_manager.list_zones() if deps.zone_manager else []
    zone_info: list[dict[str, str]] = []
    for z in zones:
        otype = z.output_type.value if hasattr(z.output_type, "value") else str(z.output_type)
        zone_info.append({"name": z.name, "type": otype.upper()})
    data["zones"] = zone_info

    # ── Network interfaces ───────────────────────────────────────────
    ifaces: list[dict[str, str]] = []
    try:
        import socket
        import psutil
        addrs = psutil.net_if_addrs()
        for iface, addr_list in addrs.items():
            for addr in addr_list:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    ifaces.append({"name": iface, "ip": addr.address})
    except ImportError:
        try:
            import socket
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ifaces.append({"name": "unknown", "ip": ip})
        except Exception:
            pass
    except Exception:
        pass
    data["network_interfaces"] = ifaces

    # ── Recent errors (sanitized) ────────────────────────────────────
    raw_errors = recent_errors(20)
    data["recent_errors"] = _sanitize_errors(raw_errors)

    # ── Timestamp ────────────────────────────────────────────────────
    data["generated_at"] = datetime.now(timezone.utc).isoformat()

    return data


def _format_markdown(data: dict[str, Any]) -> str:
    """Format collected data as a markdown bug report template."""
    lines: list[str] = []

    lines.append("## Rapport de bug Tune Server")
    lines.append("")
    lines.append("### Description du probleme")
    lines.append("")
    lines.append("_Decrivez le probleme ici..._")
    lines.append("")
    lines.append("### Etapes pour reproduire")
    lines.append("")
    lines.append("1. ")
    lines.append("2. ")
    lines.append("3. ")
    lines.append("")
    lines.append("### Comportement attendu")
    lines.append("")
    lines.append("_Que devrait-il se passer ?_")
    lines.append("")

    # --- System info ---
    lines.append("---")
    lines.append("")
    lines.append("### Informations systeme")
    lines.append("")
    lines.append(f"- **Tune Server**: v{data.get('version', '?')}")
    lines.append(f"- **OS**: {data.get('os', '?')}")
    lines.append(f"- **Python**: {data.get('python', '?')}")
    lines.append(f"- **Architecture**: {data.get('architecture', '?')}")
    lines.append(f"- **Base de donnees**: {data.get('db_engine', '?')}")
    ffmpeg = data.get("ffmpeg_version")
    lines.append(f"- **FFmpeg**: {ffmpeg if ffmpeg else 'non disponible'}")
    lines.append("")

    # --- Audio devices ---
    audio = data.get("audio_devices")
    if audio:
        lines.append("### Peripheriques audio")
        lines.append("")
        for dev in audio:
            lines.append(f"- {dev['name']} ({dev['channels']}ch, {dev['sample_rate']}Hz)")
        lines.append("")
    elif audio is None:
        lines.append("### Peripheriques audio")
        lines.append("")
        lines.append("_sounddevice non disponible_")
        lines.append("")

    # --- Streaming services ---
    services = data.get("streaming_services", [])
    lines.append("### Services connectes")
    lines.append("")
    if services:
        lines.append(", ".join(services))
    else:
        lines.append("_Aucun service connecte_")
    lines.append("")

    # --- Music dirs ---
    dirs = data.get("music_dirs", [])
    lines.append("### Repertoires musicaux")
    lines.append("")
    if dirs:
        for d in dirs:
            lines.append(f"- `{d}`")
    else:
        lines.append("_Aucun repertoire configure_")
    lines.append("")

    # --- Library stats ---
    lines.append("### Bibliotheque")
    lines.append("")
    lines.append(f"- **Pistes**: {data.get('tracks', 0):,}")
    lines.append(f"- **Albums**: {data.get('albums', 0):,}")
    lines.append(f"- **Artistes**: {data.get('artists', 0):,}")
    lines.append("")

    # --- Zones ---
    zones = data.get("zones", [])
    lines.append("### Zones")
    lines.append("")
    if zones:
        for z in zones:
            lines.append(f"- {z['name']} ({z['type']})")
    else:
        lines.append("_Aucune zone_")
    lines.append("")

    # --- Network ---
    ifaces = data.get("network_interfaces", [])
    lines.append("### Reseau")
    lines.append("")
    if ifaces:
        for iface in ifaces:
            lines.append(f"- {iface['name']}: {iface['ip']}")
    else:
        lines.append("_Aucune interface detectee_")
    lines.append("")

    # --- Recent errors ---
    errors = data.get("recent_errors", [])
    if errors:
        lines.append("### Erreurs recentes")
        lines.append("")
        lines.append("```")
        for err in errors:
            ts = err.get("ts", "?")
            level = err.get("level", "?").upper()
            event = err.get("event", "")
            lines.append(f"[{ts}] {level}: {event}")
        lines.append("```")
        lines.append("")

    lines.append(f"_Rapport genere le {data.get('generated_at', '?')}_")

    return "\n".join(lines)


def _render_html(markdown_text: str, data: dict[str, Any]) -> str:
    """Render a self-contained HTML page with the bug report and copy button."""
    # Escape for safe embedding in HTML
    import html
    escaped_md = html.escape(markdown_text)
    version = html.escape(str(data.get("version", "?")))

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tune Server - Rapport de bug</title>
<style>
  :root {{
    --bg: #1a1a2e;
    --surface: #16213e;
    --text: #e0e0e0;
    --accent: #0f3460;
    --highlight: #e94560;
    --success: #4ecca3;
    --border: #2a2a4a;
    --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 2rem;
    line-height: 1.6;
  }}
  .container {{ max-width: 800px; margin: 0 auto; }}
  h1 {{
    font-size: 1.5rem;
    margin-bottom: 1rem;
    color: var(--highlight);
  }}
  .toolbar {{
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }}
  button {{
    padding: 0.6rem 1.2rem;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--accent);
    color: var(--text);
    cursor: pointer;
    font-size: 0.9rem;
    transition: background 0.2s, border-color 0.2s;
  }}
  button:hover {{ background: var(--highlight); border-color: var(--highlight); }}
  button.copied {{
    background: var(--success);
    border-color: var(--success);
    color: #1a1a2e;
  }}
  .report {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.5rem;
    white-space: pre-wrap;
    font-family: var(--mono);
    font-size: 0.85rem;
    line-height: 1.5;
    overflow-x: auto;
    user-select: all;
  }}
  .footer {{
    margin-top: 1rem;
    font-size: 0.8rem;
    color: #666;
    text-align: center;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>Tune Server v{version} &mdash; Rapport de bug</h1>
  <div class="toolbar">
    <button id="copy-btn" onclick="copyReport()">Copier dans le presse-papiers</button>
    <button onclick="window.location.href='/api/v1/system/bug-report'">Telecharger (Markdown)</button>
    <button onclick="window.location.href='/api/v1/system/bug-report?format=json'">Telecharger (JSON)</button>
  </div>
  <pre class="report" id="report">{escaped_md}</pre>
  <p class="footer">Collez ce rapport dans un nouveau sujet sur le forum mozaiklabs.fr</p>
</div>
<script>
function copyReport() {{
  const text = document.getElementById('report').textContent;
  navigator.clipboard.writeText(text).then(() => {{
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copie !';
    btn.classList.add('copied');
    setTimeout(() => {{
      btn.textContent = 'Copier dans le presse-papiers';
      btn.classList.remove('copied');
    }}, 2000);
  }}).catch(() => {{
    // Fallback for older browsers / non-HTTPS
    const range = document.createRange();
    range.selectNodeContents(document.getElementById('report'));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copie !';
    btn.classList.add('copied');
    setTimeout(() => {{
      btn.textContent = 'Copier dans le presse-papiers';
      btn.classList.remove('copied');
    }}, 2000);
  }});
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/bug-report")
async def bug_report(format: str = Query("markdown", pattern="^(markdown|json)$")):
    """Generate a pre-filled bug report for the mozaiklabs.fr forum.

    - ``?format=markdown`` (default) — returns markdown text ready to paste.
    - ``?format=json`` — returns raw structured data.
    """
    data = await _collect_bug_report_data()

    if format == "json":
        return data

    md = _format_markdown(data)
    return PlainTextResponse(md, media_type="text/plain; charset=utf-8")


@router.get("/bug-report/html")
async def bug_report_html():
    """HTML page with the bug report and a copy-to-clipboard button."""
    data = await _collect_bug_report_data()
    md = _format_markdown(data)
    html_content = _render_html(md, data)
    return HTMLResponse(html_content)
