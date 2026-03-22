from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import structlog

from tune_server.models import (
    MediaServerBrowseResult,
    MediaServerContainer,
    MediaServerItem,
)

logger = structlog.get_logger()

# DIDL-Lite XML namespaces
NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
    "r": "urn:schemas-rinconnetworks-com:metadata-1-0/",
}

# Duration pattern: H+:MM:SS or H+:MM:SS.F+
_DURATION_RE = re.compile(r"(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")


def _parse_duration_ms(duration_str: str | None) -> int:
    """Parse UPnP duration string (H+:MM:SS.F+) to milliseconds."""
    if not duration_str:
        return 0
    m = _DURATION_RE.match(duration_str.strip())
    if not m:
        return 0
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = int(m.group(4)) if m.group(4) else 0
    # Fractional part: treat as decimal (e.g. ".50" = 500ms for 2-digit, ".5" = 500ms for 1-digit)
    frac_str = m.group(4) or "0"
    frac_ms = int(float(f"0.{frac_str}") * 1000)
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + frac_ms


class MediaServerBrowser:
    """Browse DLNA MediaServer content via ContentDirectory:1 service."""

    def __init__(self, upnp_device) -> None:
        self._device = upnp_device

    def _find_content_directory_service(self):
        """Find the ContentDirectory service on the UPnP device."""
        for service in self._device.services.values():
            if "ContentDirectory" in service.service_type:
                return service
        raise ValueError(f"No ContentDirectory service found on {self._device.friendly_name}")

    async def browse(
        self,
        object_id: str = "0",
        start: int = 0,
        count: int = 100,
    ) -> MediaServerBrowseResult:
        """Call Browse on ContentDirectory:1 and parse the DIDL-Lite XML result."""
        cd = self._find_content_directory_service()
        browse_action = cd.action("Browse")

        result = await browse_action.async_call(
            ObjectID=object_id,
            BrowseFlag="BrowseDirectChildren",
            Filter="*",
            StartingIndex=start,
            RequestedCount=count,
            SortCriteria="",
        )

        didl_xml = result.get("Result", "")
        total_matches = int(result.get("TotalMatches", 0))
        number_returned = int(result.get("NumberReturned", 0))

        return self._parse_didl(
            didl_xml,
            object_id=object_id,
            total=total_matches,
            returned=number_returned,
        )

    def _parse_didl(
        self,
        xml_str: str,
        object_id: str,
        total: int,
        returned: int,
    ) -> MediaServerBrowseResult:
        """Parse DIDL-Lite XML into containers and items."""
        containers: list[MediaServerContainer] = []
        items: list[MediaServerItem] = []

        if not xml_str or not xml_str.strip():
            return MediaServerBrowseResult(
                object_id=object_id,
                total_matches=total,
                number_returned=returned,
            )

        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            logger.warning("didl_parse_error", object_id=object_id)
            return MediaServerBrowseResult(
                object_id=object_id,
                total_matches=total,
                number_returned=returned,
            )

        # Parse <container> elements
        for elem in root.findall("didl:container", NS):
            cid = elem.get("id", "")
            parent_id = elem.get("parentID", "")
            title_el = elem.find("dc:title", NS)
            title = title_el.text if title_el is not None and title_el.text else ""
            child_count = int(elem.get("childCount", "0"))
            art_el = elem.find("upnp:albumArtURI", NS)
            album_art_uri = art_el.text if art_el is not None and art_el.text else None

            containers.append(MediaServerContainer(
                id=cid,
                parent_id=parent_id,
                title=title,
                child_count=child_count,
                album_art_uri=album_art_uri,
            ))

        # Parse <item> elements
        for elem in root.findall("didl:item", NS):
            iid = elem.get("id", "")
            parent_id = elem.get("parentID", "")
            title_el = elem.find("dc:title", NS)
            title = title_el.text if title_el is not None and title_el.text else ""

            artist_el = elem.find("upnp:artist", NS)
            if artist_el is None:
                artist_el = elem.find("dc:creator", NS)
            artist = artist_el.text if artist_el is not None and artist_el.text else None

            album_el = elem.find("upnp:album", NS)
            album = album_el.text if album_el is not None and album_el.text else None

            art_el = elem.find("upnp:albumArtURI", NS)
            album_art_uri = art_el.text if art_el is not None and art_el.text else None

            # <res> element contains the stream URL and duration
            res_el = elem.find("didl:res", NS)
            res_url = None
            duration_ms = 0
            if res_el is not None:
                res_url = res_el.text if res_el.text else None
                duration_ms = _parse_duration_ms(res_el.get("duration"))

            items.append(MediaServerItem(
                id=iid,
                parent_id=parent_id,
                title=title,
                artist=artist,
                album=album,
                res_url=res_url,
                duration_ms=duration_ms,
                album_art_uri=album_art_uri,
            ))

        return MediaServerBrowseResult(
            object_id=object_id,
            containers=containers,
            items=items,
            total_matches=total,
            number_returned=returned,
        )
