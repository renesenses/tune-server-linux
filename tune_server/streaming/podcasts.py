"""Podcast service — search, browse, and stream podcasts.

Uses iTunes Search API for catalog search, RSS feeds for episodes,
and Radio France Open API (GraphQL) for Radio France podcasts.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import aiohttp
import structlog

logger = structlog.get_logger()

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
RADIOFRANCE_GRAPHQL_URL = "https://openapi.radiofrance.fr/v1/graphql"

# Radio France stations
RF_STATIONS = ["FRANCEINTER", "FRANCECULTURE", "FRANCEMUSIQUE", "FIP", "MOUV", "FRANCEINFO"]

# Station logo URLs (from iTunes — always accessible, high-res)
RF_STATION_LOGOS = {
    "FRANCEINTER": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts116/v4/40/57/c9/4057c986-9e9b-2f27-471e-669790b9788b/mza_16146822716049881226.jpg/600x600bb.jpg",
    "FRANCECULTURE": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts126/v4/58/6b/0b/586b0b16-ef9d-d160-5593-eee87d567358/mza_4954724521098021025.jpg/600x600bb.jpg",
    "FRANCEMUSIQUE": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts115/v4/52/98/63/52986395-b451-c974-4781-a1aad9bf09c5/mza_2035646446873974817.jpg/600x600bb.jpg",
    "FIP": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts211/v4/5a/5b/c1/5a5bc1ee-a965-d95b-c074-edc0f0fddbfc/mza_12768080290056813898.jpg/600x600bb.jpg",
    "MOUV": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts221/v4/ba/10/9f/ba109f72-d875-27a7-f46d-74f6668f721b/mza_4429136207653695156.jpg/600x600bb.jpg",
    "FRANCEINFO": "https://is1-ssl.mzstatic.com/image/thumb/Podcasts122/v4/bd/3a/6b/bd3a6b04-b759-5511-6cf5-83cd52630baf/mza_12873134609460958889.jpg/600x600bb.jpg",
}


class PodcastEpisode:
    def __init__(self, title: str, description: str = "", audio_url: str = "",
                 duration_ms: int = 0, published: str = "", cover_url: str = ""):
        self.title = title
        self.description = description
        self.audio_url = audio_url
        self.duration_ms = duration_ms
        self.published = published
        self.cover_url = cover_url

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "audio_url": self.audio_url,
            "duration_ms": self.duration_ms,
            "published": self.published,
            "cover_url": self.cover_url,
        }


class Podcast:
    def __init__(self, name: str, artist: str = "", feed_url: str = "",
                 cover_url: str = "", description: str = "", episode_count: int = 0,
                 source_id: str = ""):
        self.name = name
        self.artist = artist
        self.feed_url = feed_url
        self.cover_url = cover_url
        self.description = description
        self.episode_count = episode_count
        self.source_id = source_id or hashlib.md5(feed_url.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "artist": self.artist,
            "feed_url": self.feed_url,
            "cover_url": self.cover_url,
            "description": self.description,
            "episode_count": self.episode_count,
        }


# --- Well-known Radio France podcast feeds ---

RADIO_FRANCE_PODCASTS = [
    Podcast("La Science, CQFD", "France Culture", "https://radiofrance-podcast.net/podcast09/rss_14312.xml",
            cover_url=RF_STATION_LOGOS["FRANCECULTURE"], description="Sciences et recherche"),  # noqa: E501
    Podcast("Les Pieds sur terre", "France Culture", "https://radiofrance-podcast.net/podcast09/rss_10078.xml",
            cover_url=RF_STATION_LOGOS["FRANCECULTURE"], description="Reportages et témoignages"),
    Podcast("Le Masque et la Plume", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_14007.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="Critiques cinéma, littérature, théâtre"),
    Podcast("Affaires sensibles", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_13915.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="Grandes affaires criminelles et judiciaires"),
    Podcast("La Terre au carré", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_16361.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="Environnement et écologie"),
    Podcast("Le 7/9", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_10241.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="La matinale de France Inter"),
    Podcast("Grand bien vous fasse", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_18722.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="Société et bien-être"),
    Podcast("Par Jupiter !", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_16929.xml",
            cover_url=RF_STATION_LOGOS["FRANCEINTER"], description="Humour et actualité"),
    Podcast("FIP 360", "FIP", "https://radiofrance-podcast.net/podcast09/rss_23357.xml",
            cover_url=RF_STATION_LOGOS["FIP"], description="Musique et découvertes"),
    Podcast("Certains l'aiment Fip", "FIP", "https://radiofrance-podcast.net/podcast09/rss_23187.xml",
            cover_url=RF_STATION_LOGOS["FIP"], description="Interviews et sessions musicales"),
]


class PodcastService:
    """Podcast search, browse, and episode listing."""

    def _rf_api_key(self) -> str | None:
        """Get Radio France API key from config."""
        try:
            from tune_server.config import settings
            return settings.radiofrance_api_key
        except Exception:
            return None

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search podcasts via iTunes Search API."""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    "term": query,
                    "media": "podcast",
                    "limit": min(limit, 50),
                    "country": "FR",
                }
                async with session.get(ITUNES_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)

                results = []
                for r in data.get("results", []):
                    podcast = Podcast(
                        name=r.get("trackName", ""),
                        artist=r.get("artistName", ""),
                        feed_url=r.get("feedUrl", ""),
                        cover_url=r.get("artworkUrl600", r.get("artworkUrl100", "")),
                        description=r.get("description", ""),
                        episode_count=r.get("trackCount", 0),
                        source_id=str(r.get("trackId", "")),
                    )
                    results.append(podcast.to_dict())
                return results
        except Exception:
            logger.exception("podcast_search_error")
            return []

    async def get_radio_france_podcasts(self) -> list[dict]:
        """List Radio France shows via Open API, fallback to curated RSS list."""
        api_key = self._rf_api_key()
        if not api_key:
            return [p.to_dict() for p in RADIO_FRANCE_PODCASTS]

        try:
            shows = []
            async with aiohttp.ClientSession() as session:
                for station in RF_STATIONS:
                    query = f'''{{
                        shows(station: {station}, first: 20) {{
                            edges {{ node {{
                                id title url standFirst
                                podcast {{ rss }}
                            }} }}
                        }}
                    }}'''
                    async with session.post(
                        RADIOFRANCE_GRAPHQL_URL,
                        json={"query": query},
                        headers={"x-token": api_key},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                    edges = data.get("data", {}).get("shows", {}).get("edges", [])
                    station_name = station.replace("FRANCE", "France ").replace("FIP", "FIP").replace("MOUV", "Mouv'").strip()
                    station_logo = RF_STATION_LOGOS.get(station, "")
                    for e in edges:
                        node = e["node"]
                        rss = (node.get("podcast") or {}).get("rss", "")
                        shows.append(Podcast(
                            name=node["title"],
                            artist=station_name,
                            feed_url=rss or "",
                            cover_url=station_logo,
                            description=(node.get("standFirst") or "")[:200],
                            source_id=node["id"],
                        ).to_dict())
                        # Store show URL for later episode lookup
                        shows[-1]["show_url"] = node.get("url", "")

            # Enrich covers from RSS channel images (async, best-effort)
            async with aiohttp.ClientSession() as session:
                for show in shows:
                    rss_url = show.get("feed_url", "")
                    if not rss_url or show.get("cover_url"):
                        continue
                    try:
                        async with session.get(rss_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                xml_text = await resp.text()
                                cover = self._extract_channel_image(xml_text)
                                if cover:
                                    show["cover_url"] = cover
                    except Exception:
                        pass

            logger.info("radiofrance_shows_loaded", count=len(shows))
            return shows if shows else [p.to_dict() for p in RADIO_FRANCE_PODCASTS]
        except Exception:
            logger.exception("radiofrance_api_error")
            return [p.to_dict() for p in RADIO_FRANCE_PODCASTS]

    async def get_episodes(self, feed_url: str, limit: int = 30,
                           show_url: str | None = None) -> list[dict]:
        """Fetch episodes — via Radio France API if show_url provided, else RSS."""
        # Try Radio France API first if we have a show URL
        if show_url:
            api_key = self._rf_api_key()
            if api_key:
                episodes = await self._get_rf_episodes(show_url, api_key, limit)
                if episodes:
                    return episodes

        # Fallback to RSS
        if not feed_url:
            return []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return []
                    xml_text = await resp.text()

            return self._parse_rss(xml_text, limit)
        except Exception:
            logger.exception("podcast_episodes_error", feed_url=feed_url)
            return []

    async def _get_rf_episodes(self, show_url: str, api_key: str, limit: int) -> list[dict]:
        """Fetch episodes from Radio France Open API."""
        try:
            query = f'''{{
                diffusionsOfShowByUrl(url: "{show_url}", first: {min(limit, 50)}) {{
                    edges {{ node {{
                        title standFirst published_date
                        podcastEpisode {{ url duration }}
                        show {{ title }}
                    }} }}
                }}
            }}'''
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    RADIOFRANCE_GRAPHQL_URL,
                    json={"query": query},
                    headers={"x-token": api_key},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            edges = data.get("data", {}).get("diffusionsOfShowByUrl", {}).get("edges", [])
            episodes = []
            for e in edges:
                node = e["node"]
                ep = node.get("podcastEpisode") or {}
                audio_url = ep.get("url", "")
                if not audio_url:
                    continue
                # published_date is a Unix timestamp string
                published = ""
                ts = node.get("published_date")
                if ts:
                    try:
                        published = datetime.fromtimestamp(int(ts)).strftime("%a, %d %b %Y")
                    except Exception:
                        pass
                episodes.append(PodcastEpisode(
                    title=node.get("title", ""),
                    description=(node.get("standFirst") or "")[:500],
                    audio_url=audio_url,
                    duration_ms=(ep.get("duration") or 0) * 1000,
                    published=published,
                ).to_dict())

            logger.info("radiofrance_episodes_loaded", count=len(episodes), show_url=show_url[:60])
            return episodes
        except Exception:
            logger.exception("radiofrance_episodes_error", show_url=show_url[:60])
            return []

    def _parse_rss(self, xml_text: str, limit: int) -> list[dict]:
        """Parse RSS feed XML into episodes."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []

        ns = {
            "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        }

        channel = root.find("channel")
        if channel is None:
            return []

        # Channel cover
        channel_image = ""
        itunes_image = channel.find("itunes:image", ns)
        if itunes_image is not None:
            channel_image = itunes_image.get("href", "")
        if not channel_image:
            image_el = channel.find("image/url")
            if image_el is not None:
                channel_image = image_el.text or ""

        episodes = []
        for item in channel.findall("item")[:limit]:
            title = (item.findtext("title") or "").strip()

            # Audio URL from enclosure
            audio_url = ""
            enclosure = item.find("enclosure")
            if enclosure is not None:
                audio_url = enclosure.get("url", "")

            # Duration
            duration_ms = 0
            duration_text = item.findtext("itunes:duration", namespaces=ns) or ""
            duration_ms = self._parse_duration(duration_text)

            # Description
            description = (item.findtext("itunes:summary", namespaces=ns)
                           or item.findtext("description") or "").strip()
            # Remove HTML tags
            if "<" in description:
                import re
                description = re.sub(r"<[^>]+>", "", description).strip()

            # Published date
            published = item.findtext("pubDate") or ""

            # Episode cover
            ep_image = channel_image
            itunes_ep_image = item.find("itunes:image", ns)
            if itunes_ep_image is not None:
                ep_image = itunes_ep_image.get("href", ep_image)

            if title and audio_url:
                episodes.append(PodcastEpisode(
                    title=title,
                    description=description[:500],
                    audio_url=audio_url,
                    duration_ms=duration_ms,
                    published=published,
                    cover_url=ep_image,
                ).to_dict())

        return episodes

    @staticmethod
    def _extract_channel_image(xml_text: str) -> str:
        """Extract channel cover image from RSS XML."""
        try:
            root = ET.fromstring(xml_text)
            ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
            channel = root.find("channel")
            if channel is None:
                return ""
            itunes_img = channel.find("itunes:image", ns)
            if itunes_img is not None:
                return itunes_img.get("href", "")
            img_url = channel.find("image/url")
            if img_url is not None:
                return img_url.text or ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _parse_duration(text: str) -> int:
        """Parse duration like '01:23:45' or '3600' into milliseconds."""
        if not text:
            return 0
        try:
            if ":" in text:
                parts = text.split(":")
                if len(parts) == 3:
                    return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
                elif len(parts) == 2:
                    return (int(parts[0]) * 60 + int(parts[1])) * 1000
            return int(text) * 1000
        except (ValueError, IndexError):
            return 0
