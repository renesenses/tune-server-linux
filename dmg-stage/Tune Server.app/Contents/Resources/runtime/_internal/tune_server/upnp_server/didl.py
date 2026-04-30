"""DIDL-Lite XML generation for UPnP ContentDirectory responses."""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from tune_server.audio.formats import mime_type_for_format
from tune_server.models import Album, Artist, AudioFormat, Track

# Strip characters illegal in XML 1.0 (control chars except \t \n \r)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_safe(text: str | None) -> str:
    if not text:
        return ""
    return _XML_ILLEGAL_RE.sub("", text)

DIDL_HEADER = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
)
DIDL_FOOTER = "</DIDL-Lite>"


def wrap_didl(items_xml: str) -> str:
    return f"{DIDL_HEADER}{items_xml}{DIDL_FOOTER}"


def duration_hms(ms: int | None) -> str:
    if not ms or ms <= 0:
        return "0:00:00"
    total_s = ms // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _cover_url(cover_path: str | None, server_ip: str, api_port: int) -> str | None:
    if not cover_path:
        return None
    if cover_path.startswith("http"):
        return cover_path
    filename = cover_path.replace("\\", "/").rsplit("/", 1)[-1] if ("/" in cover_path or "\\" in cover_path) else cover_path
    return f"http://{server_ip}:{api_port}/api/v1/library/artwork/{filename}"


def track_to_didl(
    track: Track,
    server_ip: str,
    http_port: int,
    api_port: int,
) -> str:
    fmt = AudioFormat(track.format) if track.format else AudioFormat.FLAC
    mime = mime_type_for_format(fmt)
    # Tracks locaux → servir via notre handler; tracks distants (UPnP) → URL directe
    if track.file_path and track.file_path.startswith("http"):
        audio_url = track.file_path
    else:
        audio_url = f"http://{server_ip}:{http_port}/upnp/audio/{track.id}"
    parent_id = f"album/{track.album_id}" if track.album_id else "tracks"

    res_attrs = f'protocolInfo="http-get:*:{mime}:*"'
    if track.duration_ms:
        res_attrs += f' duration="{duration_hms(track.duration_ms)}"'
    if track.sample_rate:
        res_attrs += f' sampleFrequency="{track.sample_rate}"'
    if track.bit_depth:
        res_attrs += f' bitsPerSample="{track.bit_depth}"'
    if track.channels:
        res_attrs += f' nrAudioChannels="{track.channels}"'

    art = _cover_url(track.cover_path, server_ip, api_port)
    art_tag = f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>" if art else ""

    return (
        f'<item id="track/{track.id}" parentID="{escape(parent_id)}" restricted="true">'
        f"<dc:title>{escape(_xml_safe(track.title))}</dc:title>"
        f"<dc:creator>{escape(_xml_safe(track.artist_name))}</dc:creator>"
        f"<upnp:artist>{escape(_xml_safe(track.artist_name))}</upnp:artist>"
        f"<upnp:album>{escape(_xml_safe(track.album_title))}</upnp:album>"
        f"<upnp:originalTrackNumber>{track.track_number or 0}</upnp:originalTrackNumber>"
        f"{art_tag}"
        f"<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<res {res_attrs}>{escape(audio_url)}</res>"
        f"</item>"
    )


def album_to_didl(
    album: Album,
    server_ip: str,
    api_port: int,
    parent_id: str = "albums",
) -> str:
    art = _cover_url(album.cover_path, server_ip, api_port)
    art_tag = f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>" if art else ""

    return (
        f'<container id="album/{album.id}" parentID="{escape(parent_id)}" '
        f'childCount="{album.track_count or 0}" restricted="true">'
        f"<dc:title>{escape(_xml_safe(album.title))}</dc:title>"
        f"<upnp:artist>{escape(_xml_safe(album.artist_name))}</upnp:artist>"
        f"{art_tag}"
        f"<upnp:class>object.container.album.musicAlbum</upnp:class>"
        f"</container>"
    )


def artist_to_didl(artist: Artist, parent_id: str = "artists") -> str:
    return (
        f'<container id="artist/{artist.id}" parentID="{escape(parent_id)}" restricted="true">'
        f"<dc:title>{escape(_xml_safe(artist.name))}</dc:title>"
        f"<upnp:class>object.container.person.musicArtist</upnp:class>"
        f"</container>"
    )


def container_to_didl(
    object_id: str,
    title: str,
    parent_id: str = "0",
    child_count: int = 0,
) -> str:
    return (
        f'<container id="{escape(object_id)}" parentID="{escape(parent_id)}" '
        f'childCount="{child_count}" restricted="true">'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<upnp:class>object.container.storageFolder</upnp:class>"
        f"</container>"
    )
