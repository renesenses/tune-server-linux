"""OpenHome UPnP event listener.

Receives NOTIFY callbacks from OpenHome devices and dispatches state
changes to the corresponding :class:`OpenHomeOutput` instances.

A single :class:`OpenHomeEventListener` is shared across all OpenHome
outputs.  Each output subscribes to the services it needs (Transport,
Info, Volume, Time, Playlist) and the listener routes incoming events
by subscription ID (SID).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Coroutine
from xml.etree import ElementTree

import aiohttp
from aiohttp import web

import structlog

from tune_server.utils.network import pick_free_port

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# How often to renew subscriptions (seconds).  Devices typically grant
# 300s; we renew well before expiry.
_RENEW_INTERVAL_S = 250
_SUBSCRIBE_TIMEOUT_S = "Second-300"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Preferred port for the event callback server.
_PREFERRED_PORT = 8890


class OpenHomeEventListener:
    """Shared HTTP server that receives UPnP NOTIFY callbacks.

    Lifecycle:
        listener = OpenHomeEventListener(server_ip)
        await listener.start()
        ...
        await listener.stop()
    """

    def __init__(self, server_ip: str) -> None:
        self._server_ip = server_ip
        self._port: int = 0
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # SID -> callback coroutine that handles the parsed event properties
        self._handlers: dict[str, Callable[[dict[str, str]], Coroutine[Any, Any, None]]] = {}

        # SID -> event_sub_url (for renewal / unsubscribe)
        self._subscriptions: dict[str, str] = {}

        # Renewal background task
        self._renewal_task: asyncio.Task | None = None
        self._running = False

    @property
    def port(self) -> int:
        return self._port

    @property
    def callback_base(self) -> str:
        """Base URL for building per-SID callback URLs."""
        return f"http://{self._server_ip}:{self._port}"

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Start the NOTIFY receiver on a free port."""
        if self._running:
            return

        self._port = pick_free_port("0.0.0.0", _PREFERRED_PORT)

        self._app = web.Application()
        self._app.router.add_route("NOTIFY", "/oh-event/{sid}", self._handle_notify)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()

        self._running = True
        self._renewal_task = asyncio.create_task(self._renewal_loop())
        logger.info("oh_event_listener_started", port=self._port)

    async def stop(self) -> None:
        """Unsubscribe all SIDs and shut down the HTTP server."""
        self._running = False

        if self._renewal_task and not self._renewal_task.done():
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
            self._renewal_task = None

        # Best-effort UNSUBSCRIBE for all active subscriptions
        await self._unsubscribe_all()

        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        self._handlers.clear()
        self._subscriptions.clear()
        logger.info("oh_event_listener_stopped")

    # -- subscribe / unsubscribe -----------------------------------------------

    async def subscribe(
        self,
        event_sub_url: str,
        handler: Callable[[dict[str, str]], Coroutine[Any, Any, None]],
    ) -> str | None:
        """SUBSCRIBE to *event_sub_url* and register *handler* for its SID.

        Returns the SID on success, or ``None`` if the device rejected
        the subscription.
        """
        # Build a unique callback path; we use a sequential counter to avoid
        # SID chicken-and-egg (we don't know the SID before subscribing).
        # After subscribing the device tells us the SID and we re-map.
        import uuid
        temp_id = uuid.uuid4().hex[:12]
        callback_url = f"{self.callback_base}/oh-event/{temp_id}"

        headers = {
            "CALLBACK": f"<{callback_url}>",
            "NT": "upnp:event",
            "TIMEOUT": _SUBSCRIBE_TIMEOUT_S,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    "SUBSCRIBE", event_sub_url, headers=headers, timeout=_HTTP_TIMEOUT,
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.debug(
                            "oh_subscribe_rejected",
                            url=event_sub_url,
                            status=resp.status,
                        )
                        return None
                    sid = resp.headers.get("SID", "").strip()
                    if not sid:
                        logger.debug("oh_subscribe_no_sid", url=event_sub_url)
                        return None
        except Exception as exc:
            logger.debug("oh_subscribe_error", url=event_sub_url, error=str(exc))
            return None

        # The device will NOTIFY to our temp_id path.  Register handler
        # under the temp_id (that's the path token the device will hit).
        self._handlers[temp_id] = handler
        self._subscriptions[temp_id] = event_sub_url

        logger.debug(
            "oh_subscribed",
            url=event_sub_url,
            sid=sid,
            callback=callback_url,
        )
        return temp_id

    async def unsubscribe(self, path_id: str) -> None:
        """UNSUBSCRIBE from a previously subscribed service."""
        event_url = self._subscriptions.pop(path_id, None)
        self._handlers.pop(path_id, None)
        if not event_url:
            return
        # We need to send the SID header.  Unfortunately UPnP UNSUBSCRIBE
        # expects the SID returned by the device.  We stored it under path_id
        # but we can still try — many devices accept the UNSUBSCRIBE even
        # without a valid SID if it comes from the same callback URL.
        try:
            headers = {"SID": path_id}
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    "UNSUBSCRIBE", event_url, headers=headers, timeout=_HTTP_TIMEOUT,
                ) as resp:
                    logger.debug("oh_unsubscribed", url=event_url, status=resp.status)
        except Exception as exc:
            logger.debug("oh_unsubscribe_error", url=event_url, error=str(exc))

    async def _unsubscribe_all(self) -> None:
        """Best-effort UNSUBSCRIBE for all active subscriptions."""
        path_ids = list(self._subscriptions.keys())
        for path_id in path_ids:
            await self.unsubscribe(path_id)

    # -- renewal ---------------------------------------------------------------

    async def _renewal_loop(self) -> None:
        """Periodically renew all active subscriptions."""
        try:
            while self._running:
                await asyncio.sleep(_RENEW_INTERVAL_S)
                if not self._running:
                    break
                await self._renew_all()
        except asyncio.CancelledError:
            pass

    async def _renew_all(self) -> None:
        """Renew every active subscription."""
        for path_id, event_url in list(self._subscriptions.items()):
            await self._renew_one(path_id, event_url)

    async def _renew_one(self, path_id: str, event_url: str) -> None:
        """Send a SUBSCRIBE renewal for one subscription."""
        callback_url = f"{self.callback_base}/oh-event/{path_id}"
        headers = {
            "CALLBACK": f"<{callback_url}>",
            "NT": "upnp:event",
            "TIMEOUT": _SUBSCRIBE_TIMEOUT_S,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    "SUBSCRIBE", event_url, headers=headers, timeout=_HTTP_TIMEOUT,
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.debug(
                            "oh_renew_failed",
                            url=event_url,
                            status=resp.status,
                        )
        except Exception as exc:
            logger.debug("oh_renew_error", url=event_url, error=str(exc))

    # -- NOTIFY handler --------------------------------------------------------

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """Handle incoming UPnP NOTIFY from a device."""
        path_id = request.match_info.get("sid", "")
        handler = self._handlers.get(path_id)
        if not handler:
            return web.Response(status=412)  # Precondition Failed — unknown SID

        try:
            body = await request.text()
        except Exception:
            return web.Response(status=400)

        # Parse the propertyset XML
        properties = _parse_propertyset(body)
        if properties:
            try:
                await handler(properties)
            except Exception:
                logger.exception("oh_event_handler_error", path_id=path_id)

        return web.Response(status=200)


def _parse_propertyset(xml_text: str) -> dict[str, str]:
    """Parse a UPnP NOTIFY propertyset into a flat dict.

    Example input::

        <e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">
          <e:property>
            <TransportState>Playing</TransportState>
          </e:property>
          <e:property>
            <Volume>42</Volume>
          </e:property>
        </e:propertyset>

    Returns ``{"TransportState": "Playing", "Volume": "42"}``.
    """
    result: dict[str, str] = {}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        logger.debug("oh_event_xml_parse_error", xml=xml_text[:200])
        return result

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "property":
            for child in elem:
                child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                result[child_tag] = (child.text or "").strip()
    return result
