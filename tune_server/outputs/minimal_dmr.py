"""Minimal DLNA MediaRenderer for devices whose XML description is unavailable.

Some audiophile renderers (e.g. DarTZeel) respond to SSDP but fail to serve
their XML description reliably.  This class bypasses the XML entirely: it
probes common AVTransport/RenderingControl control URLs with a lightweight
SOAP call and, if one responds, exposes the same interface as
async_upnp_client's DmrDevice so DlnaOutput can use it transparently.

As a first step the probe also attempts to fetch the XML description with a
generous timeout — if the device is simply slow (rather than broken), we
extract the real friendly name and exact control URLs from it.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import aiohttp
import structlog
from xml.sax.saxutils import escape as xml_escape

logger = structlog.get_logger()

_AVT_NS = "urn:schemas-upnp-org:service:AVTransport:1"
_RC_NS = "urn:schemas-upnp-org:service:RenderingControl:1"

_AVT_PATHS = [
    "/AVTransport/control",
    "/MediaRenderer/AVTransport/Control",
    "/upnp/control/AVTransport",
    "/ctl/AVTransport",
    "/upnp/control/rendertransport1",
    "/MediaRenderer_AVTransport/control",
    "/dev/AVTransport/ctrl",
    "/upnp/AVTransport/control",
    "/Control/AVTransport",
    "/dmr/AVTransport/ctrl",
]

_RC_PATHS = [
    "/RenderingControl/control",
    "/MediaRenderer/RenderingControl/Control",
    "/upnp/control/RenderingControl",
    "/ctl/RenderingControl",
    "/upnp/control/rendercontrol1",
    "/MediaRenderer_RenderingControl/control",
    "/dev/RenderingControl/ctrl",
    "/upnp/RenderingControl/control",
    "/Control/RenderingControl",
    "/dmr/RenderingControl/ctrl",
]


def _soap_body(ns: str, action: str, params: dict[str, str] | None = None) -> str:
    parts = ""
    for k, v in (params or {}).items():
        parts += f"<{k}>{xml_escape(str(v))}</{k}>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body><u:{action} xmlns:u=\"{ns}\">"
        f"{parts}"
        f"</u:{action}></s:Body></s:Envelope>"
    )


class MinimalDmrDevice:
    """DLNA renderer that bypasses XML description and sends SOAP directly.

    Implements the same interface consumed by DlnaOutput (name, transport
    control, volume, position queries).
    """

    def __init__(
        self, name: str, base_url: str, description_url: str | None = None
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._description_url = description_url
        self._avt_url: str | None = None
        self._rc_url: str | None = None
        self._transport_state: str = "STOPPED"
        self._media_position: float | None = None

    # ── properties expected by DlnaOutput ─────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def transport_state(self) -> str:
        return self._transport_state

    @property
    def media_position(self) -> float | None:
        return self._media_position

    @property
    def can_seek_rel_time(self) -> bool:
        return self._avt_url is not None

    @property
    def has_get_protocol_info(self) -> bool:
        return False

    @property
    def sink_protocol_info(self) -> list[str]:
        return []

    # ── probing ───────────────────────────────────────────────────────

    async def probe(self) -> bool:
        """Discover working control URLs.  Returns True if AVTransport found."""
        # 1. Try the XML description with a generous timeout — may yield
        #    the real friendly name AND exact control URLs.
        if await self._try_xml_description():
            logger.info(
                "minimal_dmr_from_xml", name=self._name,
                avt=self._avt_url, rc=self._rc_url,
            )
            return True

        # 2. Try common XML description paths (some devices serve XML at
        #    a different URL than the one advertised in SSDP).
        for xml_path in ("/description.xml", "/DeviceDescription.xml", "/rootDesc.xml", "/dmr.xml"):
            saved = self._description_url
            self._description_url = self._base_url + xml_path
            if await self._try_xml_description():
                logger.info(
                    "minimal_dmr_from_alt_xml", name=self._name,
                    avt=self._avt_url, rc=self._rc_url, path=xml_path,
                )
                return True
            self._description_url = saved

        # 3. Blind-probe common AVTransport paths.
        return await self._probe_common_paths()

    async def _try_xml_description(self) -> bool:
        if not self._description_url:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self._description_url) as resp:
                    if resp.status != 200:
                        return False
                    text = await resp.text()

            m = re.search(r"<friendlyName>([^<]+)</friendlyName>", text)
            if m:
                self._name = m.group(1)

            for block_m in re.finditer(r"<service>(.*?)</service>", text, re.DOTALL):
                block = block_m.group(1)
                ctrl = re.search(r"<controlURL>([^<]+)</controlURL>", block)
                if not ctrl:
                    continue
                path = ctrl.group(1)
                if not path.startswith("/"):
                    path = "/" + path

                if "AVTransport:1" in block:
                    self._avt_url = self._base_url + path
                elif "RenderingControl:1" in block:
                    self._rc_url = self._base_url + path

            return self._avt_url is not None
        except Exception as e:
            logger.debug("minimal_dmr_xml_fetch_failed", error=str(e))
        return False

    async def _probe_common_paths(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=5)
        soap = _soap_body(_AVT_NS, "GetTransportInfo", {"InstanceID": "0"})
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_AVT_NS}#GetTransportInfo"',
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for path in _AVT_PATHS:
                    url = self._base_url + path
                    try:
                        async with session.post(url, data=soap, headers=headers) as resp:
                            if resp.status == 200:
                                self._avt_url = url
                                logger.info("minimal_dmr_avt_probed", name=self._name, path=path)
                                break
                    except Exception:
                        continue

                if not self._avt_url:
                    return False

                soap_rc = _soap_body(
                    _RC_NS, "GetVolume",
                    {"InstanceID": "0", "Channel": "Master"},
                )
                headers_rc = {
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": f'"{_RC_NS}#GetVolume"',
                }
                for path in _RC_PATHS:
                    url = self._base_url + path
                    try:
                        async with session.post(url, data=soap_rc, headers=headers_rc) as resp:
                            if resp.status == 200:
                                self._rc_url = url
                                logger.info("minimal_dmr_rc_probed", name=self._name, path=path)
                                break
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("minimal_dmr_probe_error", name=self._name, error=str(e))
            return False

        return True

    # ── SOAP helpers ──────────────────────────────────────────────────

    async def _soap(
        self, url: str, ns: str, action: str,
        params: dict[str, str] | None = None,
    ) -> str | None:
        body = _soap_body(ns, action, params)
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{ns}#{action}"',
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=body, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    resp_body = await resp.text()
                    logger.debug(
                        "minimal_dmr_soap_error", name=self._name,
                        action=action, status=resp.status,
                        response=resp_body[:300] if resp_body else "",
                    )
        except Exception as e:
            logger.debug("minimal_dmr_soap_fail", name=self._name, action=action, error=str(e))
        return None

    # ── AVTransport actions ───────────────────────────────────────────

    async def async_set_transport_uri(
        self, uri: str, title: str = "", meta_data: str = "",
    ) -> None:
        if not self._avt_url:
            raise RuntimeError("AVTransport not available")
        await self._soap(self._avt_url, _AVT_NS, "SetAVTransportURI", {
            "InstanceID": "0",
            "CurrentURI": uri,
            "CurrentURIMetaData": meta_data,
        })

    async def async_play(self) -> None:
        if self._avt_url:
            await self._soap(self._avt_url, _AVT_NS, "Play", {
                "InstanceID": "0", "Speed": "1",
            })

    async def async_stop(self) -> None:
        if self._avt_url:
            await self._soap(self._avt_url, _AVT_NS, "Stop", {
                "InstanceID": "0",
            })

    async def async_pause(self) -> None:
        if self._avt_url:
            await self._soap(self._avt_url, _AVT_NS, "Pause", {
                "InstanceID": "0",
            })

    async def async_set_next_transport_uri(
        self, uri: str, title: str = "", meta_data: str = "",
    ) -> None:
        if not self._avt_url:
            logger.warning("minimal_dmr_set_next_no_avt", name=self._name)
            return
        result = await self._soap(self._avt_url, _AVT_NS, "SetNextAVTransportURI", {
            "InstanceID": "0",
            "NextURI": uri,
            "NextURIMetaData": meta_data,
        })
        if result is not None:
            logger.info(
                "minimal_dmr_set_next_ok", name=self._name,
                uri=uri[:80], title=title,
            )
        else:
            logger.warning(
                "minimal_dmr_set_next_failed", name=self._name,
                uri=uri[:80], title=title,
            )

    async def async_seek_rel_time(self, target: str) -> None:
        if self._avt_url:
            await self._soap(self._avt_url, _AVT_NS, "Seek", {
                "InstanceID": "0", "Unit": "REL_TIME", "Target": target,
            })

    # ── RenderingControl ──────────────────────────────────────────────

    async def async_set_volume_level(self, volume: float) -> None:
        if self._rc_url:
            await self._soap(self._rc_url, _RC_NS, "SetVolume", {
                "InstanceID": "0",
                "Channel": "Master",
                "DesiredVolume": str(int(volume * 100)),
            })

    # ── State queries ─────────────────────────────────────────────────

    async def async_update(self, do_ping: bool = False) -> None:
        if not self._avt_url:
            return
        resp = await self._soap(
            self._avt_url, _AVT_NS, "GetTransportInfo", {"InstanceID": "0"},
        )
        if resp:
            m = re.search(r"<CurrentTransportState>(\w+)</CurrentTransportState>", resp)
            if m:
                self._transport_state = m.group(1)

        resp2 = await self._soap(
            self._avt_url, _AVT_NS, "GetPositionInfo", {"InstanceID": "0"},
        )
        if resp2:
            m = re.search(r"<RelTime>([\d:.]+)</RelTime>", resp2)
            if m:
                self._media_position = self._parse_time(m.group(1))

    async def async_get_protocol_info(self) -> None:
        pass

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_time(t: str) -> float:
        parts = t.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return 0.0
