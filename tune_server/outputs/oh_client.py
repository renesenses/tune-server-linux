"""OpenHome SOAP client.

Lightweight async client for OpenHome services (Linn, Naim, Auralic,
Lumin, dCS, Cambridge Audio).  Each method sends a SOAP POST to the
device's control URL discovered from the device description XML.

Supported services
------------------
- Product  (urn:av-openhome-org:service:Product:1)
- Playlist (urn:av-openhome-org:service:Playlist:1)
- Transport(urn:av-openhome-org:service:Transport:1)
- Volume   (urn:av-openhome-org:service:Volume:1)
- Info     (urn:av-openhome-org:service:Info:1)
- Time     (urn:av-openhome-org:service:Time:1)
"""
from __future__ import annotations

import base64
from typing import Any
from xml.etree import ElementTree

import aiohttp
import structlog

logger = structlog.get_logger()

# -- Service type URNs --------------------------------------------------------

SVC_PRODUCT = "urn:av-openhome-org:service:Product:1"
SVC_PLAYLIST = "urn:av-openhome-org:service:Playlist:1"
SVC_TRANSPORT = "urn:av-openhome-org:service:Transport:1"
SVC_VOLUME = "urn:av-openhome-org:service:Volume:1"
SVC_INFO = "urn:av-openhome-org:service:Info:1"
SVC_TIME = "urn:av-openhome-org:service:Time:1"
SVC_PINS = "urn:av-openhome-org:service:Pins:1"

_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=5)


def _xml_escape(s: str) -> str:
    """Minimal XML escaping for SOAP argument values."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class OpenHomeClient:
    """Async SOAP client for a single OpenHome device.

    Parameters
    ----------
    service_urls : dict[str, str]
        Mapping of short service key to absolute control URL.
        Expected keys: ``product``, ``playlist``, ``transport``,
        ``volume``, ``info``, ``time``.  Missing keys are tolerated —
        the corresponding methods return *None* / default values.
    device_name : str
        Friendly name for logging.
    """

    def __init__(self, service_urls: dict[str, str], device_name: str = "OpenHome") -> None:
        self._urls = service_urls
        self._device_name = device_name
        self._session: aiohttp.ClientSession | None = None

    # -- lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # -- low-level SOAP --------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _soap_call(
        self,
        url: str,
        service_type: str,
        action: str,
        args: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> ElementTree.Element | None:
        """Send a SOAP POST and return the parsed XML root, or *None* on error."""
        body_args = ""
        if args:
            for k, v in args.items():
                body_args += f"<{k}>{_xml_escape(v)}</{k}>"

        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<s:Envelope xmlns:s="{_SOAP_NS}" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service_type}">'
            f'{body_args}'
            f'</u:{action}></s:Body></s:Envelope>'
        )

        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        }

        session = await self._get_session()
        try:
            async with session.post(url, data=envelope, headers=headers,
                                    timeout=timeout) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.debug("oh_soap_error", device=self._device_name,
                                 action=action, status=resp.status,
                                 body=text[:300])
                    return None
                return ElementTree.fromstring(text)
        except Exception as exc:
            logger.debug("oh_soap_exception", device=self._device_name,
                         action=action, error=str(exc))
            return None

    def _extract(self, root: ElementTree.Element | None, tag: str) -> str | None:
        """Find the first element matching *tag* (ignoring namespace) and return its text."""
        if root is None:
            return None
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == tag:
                return elem.text
        return None

    def _extract_int(self, root: ElementTree.Element | None, tag: str, default: int = 0) -> int:
        val = self._extract(root, tag)
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def _has_service(self, key: str) -> str | None:
        """Return the control URL for *key* if available, else ``None``."""
        return self._urls.get(key)

    # =========================================================================
    # Transport service
    # =========================================================================

    async def transport_play(self) -> None:
        """Start / resume playback via Transport service.

        Falls back to Playlist Play if Transport is not available.
        """
        url = self._has_service("transport")
        if url:
            await self._soap_call(url, SVC_TRANSPORT, "Play")
        else:
            # Fallback: use Playlist service
            url = self._has_service("playlist")
            if url:
                await self._soap_call(url, SVC_PLAYLIST, "Play")

    async def transport_pause(self) -> None:
        url = self._has_service("transport")
        if url:
            await self._soap_call(url, SVC_TRANSPORT, "Pause")
        else:
            url = self._has_service("playlist")
            if url:
                await self._soap_call(url, SVC_PLAYLIST, "Pause")

    async def transport_stop(self) -> None:
        url = self._has_service("transport")
        if url:
            await self._soap_call(url, SVC_TRANSPORT, "Stop")
        else:
            url = self._has_service("playlist")
            if url:
                await self._soap_call(url, SVC_PLAYLIST, "Stop")

    async def transport_seek_seconds(self, seconds: int) -> None:
        """Seek to an absolute position in the current track."""
        url = self._has_service("transport")
        if url:
            await self._soap_call(url, SVC_TRANSPORT, "SeekSecondAbsolute",
                                  {"Value": str(seconds)})
        else:
            url = self._has_service("playlist")
            if url:
                await self._soap_call(url, SVC_PLAYLIST, "SeekSecondAbsolute",
                                      {"Value": str(seconds)})

    async def transport_state(self) -> str:
        """Return the transport state: ``Playing``, ``Paused``, ``Stopped``, or ``Buffering``.

        Returns ``"Unknown"`` if the service is unavailable.
        """
        url = self._has_service("transport")
        if not url:
            return "Unknown"
        root = await self._soap_call(url, SVC_TRANSPORT, "TransportState")
        return self._extract(root, "State") or "Unknown"

    # =========================================================================
    # Playlist service
    # =========================================================================

    async def playlist_insert(self, after_id: int, uri: str, metadata: str) -> int | None:
        """Insert a track after *after_id*. Returns the new track ID or *None*."""
        url = self._has_service("playlist")
        if not url:
            return None
        root = await self._soap_call(url, SVC_PLAYLIST, "Insert", {
            "AfterId": str(after_id),
            "Uri": uri,
            "Metadata": metadata,
        })
        val = self._extract(root, "NewId")
        return int(val) if val is not None else None

    async def playlist_delete_all(self) -> None:
        """Clear the device playlist."""
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "DeleteAll")

    async def playlist_play(self) -> None:
        """Play from the current index in the playlist."""
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "Play")

    async def playlist_pause(self) -> None:
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "Pause")

    async def playlist_stop(self) -> None:
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "Stop")

    async def playlist_seek_id(self, track_id: int) -> None:
        """Seek to the track with the given playlist ID."""
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "SeekId",
                                  {"Value": str(track_id)})

    async def playlist_seek_second_absolute(self, seconds: int) -> None:
        """Seek to *seconds* within the current track."""
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "SeekSecondAbsolute",
                                  {"Value": str(seconds)})

    async def playlist_set_repeat(self, repeat: bool) -> None:
        """Enable or disable repeat mode."""
        url = self._has_service("playlist")
        if url:
            await self._soap_call(url, SVC_PLAYLIST, "SetRepeat",
                                  {"Value": "1" if repeat else "0"})

    async def playlist_id_array(self) -> list[int]:
        """Return the ordered list of track IDs in the device playlist.

        The OpenHome IdArray response is a base64-encoded array of
        big-endian 32-bit integers.
        """
        url = self._has_service("playlist")
        if not url:
            return []
        root = await self._soap_call(url, SVC_PLAYLIST, "IdArray")
        b64 = self._extract(root, "Array")
        if not b64:
            return []
        try:
            raw = base64.b64decode(b64)
            # big-endian uint32
            ids: list[int] = []
            for i in range(0, len(raw), 4):
                ids.append(int.from_bytes(raw[i:i + 4], byteorder="big"))
            return ids
        except Exception:
            return []

    async def playlist_read(self, track_id: int) -> dict[str, str] | None:
        """Read the URI and metadata for a track ID.

        Returns ``{"Uri": ..., "Metadata": ...}`` or *None*.
        """
        url = self._has_service("playlist")
        if not url:
            return None
        root = await self._soap_call(url, SVC_PLAYLIST, "Read",
                                     {"Id": str(track_id)})
        if root is None:
            return None
        uri = self._extract(root, "Uri")
        metadata = self._extract(root, "Metadata")
        if uri is None:
            return None
        return {"Uri": uri, "Metadata": metadata or ""}

    # =========================================================================
    # Volume service
    # =========================================================================

    async def volume_get(self) -> int:
        """Return the current volume level (0–100)."""
        url = self._has_service("volume")
        if not url:
            return 0
        root = await self._soap_call(url, SVC_VOLUME, "Volume")
        return self._extract_int(root, "Value", 0)

    async def volume_set(self, level: int) -> None:
        """Set the volume level (0–100)."""
        url = self._has_service("volume")
        if url:
            level = max(0, min(100, level))
            await self._soap_call(url, SVC_VOLUME, "SetVolume",
                                  {"Value": str(level)})

    async def mute_get(self) -> bool:
        """Return ``True`` if the device is muted."""
        url = self._has_service("volume")
        if not url:
            return False
        root = await self._soap_call(url, SVC_VOLUME, "Mute")
        val = self._extract(root, "Value")
        return val == "1" or (val or "").lower() == "true"

    async def mute_set(self, muted: bool) -> None:
        """Mute or unmute the device."""
        url = self._has_service("volume")
        if url:
            await self._soap_call(url, SVC_VOLUME, "SetMute",
                                  {"Value": "1" if muted else "0"})

    async def volume_limit(self) -> int:
        """Return the device's maximum volume level."""
        url = self._has_service("volume")
        if not url:
            return 100
        root = await self._soap_call(url, SVC_VOLUME, "VolumeLimit")
        return self._extract_int(root, "Value", 100)

    # =========================================================================
    # Info service
    # =========================================================================

    async def info_track(self) -> dict[str, str]:
        """Return current track URI and metadata.

        Returns ``{"Uri": ..., "Metadata": ...}``.
        """
        url = self._has_service("info")
        if not url:
            return {"Uri": "", "Metadata": ""}
        root = await self._soap_call(url, SVC_INFO, "Track")
        return {
            "Uri": self._extract(root, "Uri") or "",
            "Metadata": self._extract(root, "Metadata") or "",
        }

    async def info_details(self) -> dict[str, Any]:
        """Return playback details: sample rate, bit depth, codec, etc.

        Returns a dict with keys ``Duration``, ``BitRate``, ``BitDepth``,
        ``SampleRate``, ``Lossless``, ``CodecName``.
        """
        url = self._has_service("info")
        if not url:
            return {}
        root = await self._soap_call(url, SVC_INFO, "Details")
        if root is None:
            return {}
        return {
            "Duration": self._extract_int(root, "Duration", 0),
            "BitRate": self._extract_int(root, "BitRate", 0),
            "BitDepth": self._extract_int(root, "BitDepth", 0),
            "SampleRate": self._extract_int(root, "SampleRate", 0),
            "Lossless": (self._extract(root, "Lossless") or "").lower() in ("true", "1"),
            "CodecName": self._extract(root, "CodecName") or "",
        }

    # =========================================================================
    # Time service
    # =========================================================================

    async def time_get(self) -> tuple[int, int]:
        """Return ``(duration_seconds, position_seconds)``."""
        url = self._has_service("time")
        if not url:
            return (0, 0)
        root = await self._soap_call(url, SVC_TIME, "Time")
        duration = self._extract_int(root, "TrackDuration", 0)
        seconds = self._extract_int(root, "Seconds", 0)
        return (duration, seconds)

    # =========================================================================
    # Product service
    # =========================================================================

    async def product_set_standby(self, standby: bool) -> None:
        url = self._has_service("product")
        if url:
            await self._soap_call(url, SVC_PRODUCT, "SetStandby",
                                  {"Value": "1" if standby else "0"})

    async def product_get_standby(self) -> bool:
        url = self._has_service("product")
        if not url:
            return False
        root = await self._soap_call(url, SVC_PRODUCT, "Standby")
        val = self._extract(root, "Value")
        return val == "1" or (val or "").lower() == "true"

    async def product_set_source_index(self, index: int) -> None:
        """Select input source by index."""
        url = self._has_service("product")
        if url:
            await self._soap_call(url, SVC_PRODUCT, "SetSourceIndex",
                                  {"Value": str(index)})

    async def product_source_count(self) -> int:
        """Return the number of input sources."""
        url = self._has_service("product")
        if not url:
            return 0
        root = await self._soap_call(url, SVC_PRODUCT, "SourceCount")
        return self._extract_int(root, "Value", 0)

    async def product_source(self, index: int) -> tuple[str, str, bool]:
        """Return ``(name, type, visible)`` for a source at *index*."""
        url = self._has_service("product")
        if not url:
            return ("", "", False)
        root = await self._soap_call(url, SVC_PRODUCT, "Source",
                                     {"Index": str(index)})
        name = self._extract(root, "SystemName") or self._extract(root, "Name") or ""
        stype = self._extract(root, "Type") or ""
        visible = self._extract(root, "Visible")
        return (name, stype, visible == "1" or (visible or "").lower() == "true")

    async def product_source_xml(self) -> list[dict[str, str]]:
        """Return all sources as a list of dicts with *Name*, *Type*, *Visible*.

        Parses the XML blob returned by the ``SourceXml`` action.
        """
        url = self._has_service("product")
        if not url:
            return []
        root = await self._soap_call(url, SVC_PRODUCT, "SourceXml")
        xml_str = self._extract(root, "Value")
        if not xml_str:
            return []
        try:
            sources_root = ElementTree.fromstring(xml_str)
        except ElementTree.ParseError:
            return []
        result: list[dict[str, str]] = []
        for source in sources_root:
            entry: dict[str, str] = {}
            for child in source:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                entry[tag] = (child.text or "").strip()
            result.append(entry)
        return result

    async def select_playlist_source(self) -> bool:
        """Find the Playlist source and select it.  Returns ``True`` on success."""
        sources = await self.product_source_xml()
        for idx, src in enumerate(sources):
            if src.get("Type", "").strip() == "Playlist":
                await self.product_set_source_index(idx)
                logger.debug("oh_source_set_playlist", device=self._device_name, index=idx)
                return True
        logger.debug("oh_no_playlist_source", device=self._device_name)
        return False

    # =========================================================================
    # Pins service (Phase 5)
    # =========================================================================

    def has_pins_service(self) -> bool:
        """Return ``True`` if the device exposes the Pins service."""
        return self._has_service("pins") is not None

    async def pins_get_device_max(self) -> int:
        """Return the total number of pin slots on the device."""
        url = self._has_service("pins")
        if not url:
            return 0
        root = await self._soap_call(url, SVC_PINS, "GetDeviceMax")
        return self._extract_int(root, "DeviceMax", 0)

    async def pins_get_id_array(self) -> list[int]:
        """Return the list of pin IDs currently set on the device.

        The response is a base64-encoded array of big-endian 32-bit
        integers, same format as Playlist IdArray.
        """
        url = self._has_service("pins")
        if not url:
            return []
        root = await self._soap_call(url, SVC_PINS, "GetIdArray")
        b64 = self._extract(root, "IdArray")
        if not b64:
            return []
        try:
            raw = base64.b64decode(b64)
            ids: list[int] = []
            for i in range(0, len(raw), 4):
                ids.append(int.from_bytes(raw[i:i + 4], byteorder="big"))
            return ids
        except Exception:
            return []

    async def pins_read_list(self, ids: list[int]) -> list[dict]:
        """Read pin details for the given IDs.

        Returns a list of dicts with keys: ``id``, ``index``, ``mode``,
        ``type``, ``uri``, ``title``, ``description``, ``artworkUri``,
        ``shuffle``.
        """
        url = self._has_service("pins")
        if not url:
            return []
        id_str = ",".join(str(i) for i in ids)
        root = await self._soap_call(url, SVC_PINS, "ReadList",
                                     {"Ids": id_str})
        xml_str = self._extract(root, "List")
        if not xml_str:
            return []
        try:
            list_root = ElementTree.fromstring(f"<Pins>{xml_str}</Pins>")
        except ElementTree.ParseError:
            # Try wrapping if it's a fragment
            try:
                list_root = ElementTree.fromstring(xml_str)
            except ElementTree.ParseError:
                return []
        result: list[dict] = []
        for entry in list_root:
            pin: dict[str, Any] = {}
            for child in entry:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                text = (child.text or "").strip()
                if tag.lower() in ("id", "index"):
                    try:
                        pin[tag.lower()] = int(text)
                    except (ValueError, TypeError):
                        pin[tag.lower()] = 0
                elif tag.lower() == "shuffle":
                    pin["shuffle"] = text.lower() in ("true", "1")
                else:
                    pin[tag.lower()] = text
            if pin:
                result.append(pin)
        return result

    async def pins_set(
        self,
        index: int,
        mode: str,
        type_: str,
        uri: str,
        title: str,
        description: str = "",
        artwork_uri: str = "",
        shuffle: bool = False,
    ) -> None:
        """Set a pin at the given slot index."""
        url = self._has_service("pins")
        if not url:
            return
        await self._soap_call(url, SVC_PINS, "Set", {
            "Index": str(index),
            "Mode": mode,
            "Type": type_,
            "Uri": uri,
            "Title": title,
            "Description": description,
            "ArtworkUri": artwork_uri,
            "Shuffle": "1" if shuffle else "0",
        })

    async def pins_clear(self, id_: int) -> None:
        """Clear (remove) a pin by its ID."""
        url = self._has_service("pins")
        if not url:
            return
        await self._soap_call(url, SVC_PINS, "Clear", {"Id": str(id_)})

    async def pins_invoke_index(self, index: int) -> None:
        """Invoke (trigger playback of) the pin at the given slot index."""
        url = self._has_service("pins")
        if not url:
            return
        await self._soap_call(url, SVC_PINS, "InvokeIndex",
                              {"Index": str(index)})

    async def pins_invoke_id(self, id_: int) -> None:
        """Invoke (trigger playback of) the pin with the given ID."""
        url = self._has_service("pins")
        if not url:
            return
        await self._soap_call(url, SVC_PINS, "InvokeId",
                              {"Id": str(id_)})
