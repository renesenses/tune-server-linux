"""UPnP ConnectionManager SOAP service."""
from __future__ import annotations

from aiohttp import web

SOURCE_PROTOCOLS = ",".join([
    "http-get:*:audio/flac:*",
    "http-get:*:audio/x-flac:*",
    "http-get:*:audio/wav:*",
    "http-get:*:audio/x-wav:*",
    "http-get:*:audio/mpeg:*",
    "http-get:*:audio/mp3:*",
    "http-get:*:audio/mp4:*",
    "http-get:*:audio/aac:*",
    "http-get:*:audio/ogg:*",
    "http-get:*:audio/aiff:*",
    "http-get:*:audio/x-dsf:*",
])


def _soap_response(action: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
        f"{body}"
        f"</u:{action}Response>"
        "</s:Body>"
        "</s:Envelope>"
    )


async def handle_connection_manager(request: web.Request) -> web.Response:
    soap_action = request.headers.get("SOAPAction", "")
    if "GetProtocolInfo" in soap_action:
        body = f"<Source>{SOURCE_PROTOCOLS}</Source><Sink></Sink>"
        return web.Response(
            text=_soap_response("GetProtocolInfo", body),
            content_type="text/xml",
        )
    if "GetCurrentConnectionIDs" in soap_action:
        return web.Response(
            text=_soap_response("GetCurrentConnectionIDs", "<ConnectionIDs>0</ConnectionIDs>"),
            content_type="text/xml",
        )
    return web.Response(status=501, text="Action not implemented")


def register_cm_routes(app: web.Application) -> None:
    app.router.add_post("/upnp/control/ConnectionManager", handle_connection_manager)
