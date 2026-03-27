"""UPnP ContentDirectory SOAP service — Browse & Search."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import structlog
from aiohttp import web
from xml.sax.saxutils import escape as xml_escape

from tune_server.upnp_server.didl import (
    album_to_didl,
    artist_to_didl,
    container_to_didl,
    track_to_didl,
    wrap_didl,
)

logger = structlog.get_logger()

# Namespace map for parsing SOAP requests
NS = {
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "u": "urn:schemas-upnp-org:service:ContentDirectory:1",
}


class ContentDirectoryHandler:
    def __init__(
        self,
        track_repo,
        album_repo,
        artist_repo,
        server_ip: str,
        http_port: int,
        api_port: int,
    ) -> None:
        self._tracks = track_repo
        self._albums = album_repo
        self._artists = artist_repo
        self._ip = server_ip
        self._http_port = http_port
        self._api_port = api_port
        self.system_update_id = 1

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.text()
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return web.Response(status=400, text="Bad XML")

        # Extract action name from SOAPAction header or body
        soap_action = request.headers.get("SOAPAction", "")
        if "Browse" in soap_action:
            return await self._browse(root)
        if "Search" in soap_action:
            return await self._search(root)
        if "GetSystemUpdateID" in soap_action:
            return self._soap_ok("GetSystemUpdateID", f"<Id>{self.system_update_id}</Id>")
        if "GetSearchCapabilities" in soap_action:
            return self._soap_ok("GetSearchCapabilities", "<SearchCaps>dc:title,upnp:artist,upnp:album</SearchCaps>")
        if "GetSortCapabilities" in soap_action:
            return self._soap_ok("GetSortCapabilities", "<SortCaps>dc:title,upnp:artist,upnp:album</SortCaps>")
        return web.Response(status=501)

    # ── Browse ──────────────────────────────────────────────────────────

    async def _browse(self, root: ET.Element) -> web.Response:
        body = root.find(".//u:Browse", NS)
        if body is None:
            # Try without namespace prefix
            body = root.find(".//{urn:schemas-upnp-org:service:ContentDirectory:1}Browse")
        if body is None:
            return web.Response(status=400)

        object_id = self._arg(body, "ObjectID") or "0"
        browse_flag = self._arg(body, "BrowseFlag") or "BrowseDirectChildren"
        start = int(self._arg(body, "StartingIndex") or "0")
        count = int(self._arg(body, "RequestedCount") or "100")
        if count == 0:
            count = 200

        if browse_flag == "BrowseMetadata":
            return await self._browse_metadata(object_id)
        return await self._browse_children(object_id, start, count)

    async def _browse_metadata(self, object_id: str) -> web.Response:
        if object_id == "0":
            xml = container_to_didl("0", "Root", "-1", 4)
        elif object_id == "albums":
            total = await self._albums.count()
            xml = container_to_didl("albums", "Albums", "0", total)
        elif object_id == "artists":
            total = await self._artists.count()
            xml = container_to_didl("artists", "Artists", "0", total)
        elif object_id == "tracks":
            total = await self._tracks.count()
            xml = container_to_didl("tracks", "All Tracks", "0", total)
        elif object_id == "genres":
            xml = container_to_didl("genres", "Genres", "0")
        elif object_id.startswith("album/"):
            album_id = int(object_id.split("/")[1])
            album = await self._albums.get(album_id)
            if not album:
                return self._soap_ok("Browse", self._browse_result("", 0, 0))
            xml = album_to_didl(album, self._ip, self._api_port)
        elif object_id.startswith("artist/"):
            artist_id = int(object_id.split("/")[1])
            artist = await self._artists.get(artist_id)
            if not artist:
                return self._soap_ok("Browse", self._browse_result("", 0, 0))
            xml = artist_to_didl(artist)
        elif object_id.startswith("track/"):
            track_id = int(object_id.split("/")[1])
            track = await self._tracks.get(track_id)
            if not track:
                return self._soap_ok("Browse", self._browse_result("", 0, 0))
            xml = track_to_didl(track, self._ip, self._http_port, self._api_port)
        else:
            xml = ""
        return self._soap_ok("Browse", self._browse_result(wrap_didl(xml), 1, 1))

    async def _browse_children(self, object_id: str, start: int, count: int) -> web.Response:
        items_xml = ""
        total = 0
        returned = 0

        if object_id == "0":
            # Root children: Albums, Artists, Tracks, Genres
            containers = [
                ("albums", "Albums"),
                ("artists", "Artists"),
                ("tracks", "All Tracks"),
                ("genres", "Genres"),
            ]
            total = len(containers)
            for cid, label in containers[start:start + count]:
                items_xml += container_to_didl(cid, label, "0")
                returned += 1

        elif object_id == "albums":
            albums = await self._albums.list(limit=count, offset=start)
            total = await self._albums.count()
            for a in albums:
                items_xml += album_to_didl(a, self._ip, self._api_port)
                returned += 1

        elif object_id == "artists":
            artists = await self._artists.list(limit=count, offset=start)
            total = await self._artists.count()
            for a in artists:
                items_xml += artist_to_didl(a)
                returned += 1

        elif object_id == "tracks":
            tracks = await self._tracks.list(limit=count, offset=start)
            total = await self._tracks.count()
            for t in tracks:
                items_xml += track_to_didl(t, self._ip, self._http_port, self._api_port)
                returned += 1

        elif object_id.startswith("album/"):
            album_id = int(object_id.split("/")[1])
            tracks = await self._tracks.list_by_album(album_id)
            total = len(tracks)
            for t in tracks[start:start + count]:
                items_xml += track_to_didl(t, self._ip, self._http_port, self._api_port)
                returned += 1

        elif object_id.startswith("artist/"):
            artist_id = int(object_id.split("/")[1])
            albums = await self._albums.list_by_artist(artist_id)
            total = len(albums)
            for a in albums[start:start + count]:
                items_xml += album_to_didl(a, self._ip, self._api_port, parent_id=object_id)
                returned += 1

        elif object_id == "genres":
            genres = await self._albums.list_genres()
            total = len(genres)
            for g in genres[start:start + count]:
                items_xml += container_to_didl(f"genre/{g}", g, "genres")
                returned += 1

        elif object_id.startswith("genre/"):
            genre_name = object_id[6:]
            albums = await self._albums.list_by_genre(genre_name)
            total = len(albums)
            for a in albums[start:start + count]:
                items_xml += album_to_didl(a, self._ip, self._api_port, parent_id=object_id)
                returned += 1

        result = wrap_didl(items_xml)
        return self._soap_ok("Browse", self._browse_result(result, returned, total))

    # ── Search ──────────────────────────────────────────────────────────

    async def _search(self, root: ET.Element) -> web.Response:
        body = root.find(".//u:Search", NS) or root.find(".//{urn:schemas-upnp-org:service:ContentDirectory:1}Search")
        if body is None:
            return web.Response(status=400)

        criteria = self._arg(body, "SearchCriteria") or ""
        start = int(self._arg(body, "StartingIndex") or "0")
        count = int(self._arg(body, "RequestedCount") or "100")
        if count == 0:
            count = 200

        # Extract search term from UPnP criteria (simplified parser)
        query = self._extract_search_term(criteria)
        if not query:
            return self._soap_ok("Search", self._browse_result(wrap_didl(""), 0, 0))

        tracks = await self._tracks.search(query, limit=count, offset=start)
        total = len(tracks)  # approximate
        items_xml = ""
        for t in tracks:
            items_xml += track_to_didl(t, self._ip, self._http_port, self._api_port)

        return self._soap_ok("Search", self._browse_result(wrap_didl(items_xml), len(tracks), total))

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _arg(element: ET.Element, name: str) -> str | None:
        for child in element:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == name:
                return child.text
        return None

    @staticmethod
    def _extract_search_term(criteria: str) -> str | None:
        """Extract search string from UPnP criteria like 'dc:title contains "foo"'."""
        import re
        m = re.search(r'contains\s+"([^"]+)"', criteria, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r'=\s+"([^"]+)"', criteria)
        if m:
            return m.group(1)
        return criteria.strip() if criteria.strip() else None

    def _browse_result(self, result_xml: str, num_returned: int, total: int) -> str:
        return (
            f"<Result>{xml_escape(result_xml)}</Result>"
            f"<NumberReturned>{num_returned}</NumberReturned>"
            f"<TotalMatches>{total}</TotalMatches>"
            f"<UpdateID>{self.system_update_id}</UpdateID>"
        )

    @staticmethod
    def _soap_ok(action: str, body: str) -> web.Response:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            f"{body}"
            f"</u:{action}Response>"
            "</s:Body></s:Envelope>"
        )
        return web.Response(text=xml, content_type="text/xml")


def register_cd_routes(app: web.Application, handler: ContentDirectoryHandler) -> None:
    app.router.add_post("/upnp/control/ContentDirectory", handler.handle)
