"""Podcast service — search, browse, and stream podcasts.

Uses iTunes Search API for catalog search and RSS feeds for episodes.
Radio France API support planned (pending API key validation).
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
            description="Sciences et recherche"),
    Podcast("Les Pieds sur terre", "France Culture", "https://radiofrance-podcast.net/podcast09/rss_10078.xml",
            description="Reportages et témoignages"),
    Podcast("Le Masque et la Plume", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_14007.xml",
            description="Critiques cinéma, littérature, théâtre"),
    Podcast("Affaires sensibles", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_13915.xml",
            description="Grandes affaires criminelles et judiciaires"),
    Podcast("La Terre au carré", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_16361.xml",
            description="Environnement et écologie"),
    Podcast("Le 7/9", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_10241.xml",
            description="La matinale de France Inter"),
    Podcast("Grand bien vous fasse", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_18722.xml",
            description="Société et bien-être"),
    Podcast("Par Jupiter !", "France Inter", "https://radiofrance-podcast.net/podcast09/rss_16929.xml",
            description="Humour et actualité"),
    Podcast("FIP 360", "FIP", "https://radiofrance-podcast.net/podcast09/rss_23357.xml",
            description="Musique et découvertes"),
    Podcast("Certains l'aiment Fip", "FIP", "https://radiofrance-podcast.net/podcast09/rss_23187.xml",
            description="Interviews et sessions musicales"),
]


class PodcastService:
    """Podcast search, browse, and episode listing."""

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
                    data = await resp.json()

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
        """Return curated list of Radio France podcasts."""
        return [p.to_dict() for p in RADIO_FRANCE_PODCASTS]

    async def get_episodes(self, feed_url: str, limit: int = 30) -> list[dict]:
        """Fetch episodes from an RSS feed."""
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
