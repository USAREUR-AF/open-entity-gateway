"""An HTTP listener for first-bite's `rest` sink.

The sink POSTs one record per request, as JSON or as CBOR (its `encoding`).
The answer tells it what happened:

    204  the record became a track and was queued for every destination,
         or no mapping matches it
    422  a mapping matches and the record cannot fill it (the log names the field)
    400, 404, 411, 413, 415  the request is not a record posted to the path
"""

from __future__ import annotations

import ipaddress
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..gateway import REFUSED, Gateway

log = logging.getLogger("open_entity_gateway")

# A record carries the captured frame twice in base64; the largest frame is
# 65,535 bytes.
MAX_BODY_BYTES = 1024 * 1024
MEDIA_TYPES = ("application/json", "application/cbor")


def decode_body(media_type: str, body: bytes) -> Any:
    if media_type == "application/json":
        return json.loads(body)
    import cbor2

    return cbor2.loads(body)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(gateway: Gateway, host: str, port: int, path: str = "/records") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "open-entity-gateway"
        sys_version = ""

        def answer(self, status: int, text: str = "") -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if body:
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != path:
                return self.answer(404, f"records are POSTed to {path}\n")
            media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if media_type not in MEDIA_TYPES:
                return self.answer(415, "send records as application/json or application/cbor\n")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.answer(411, "Content-Length is required\n")
            if not 0 < length <= MAX_BODY_BYTES:
                return self.answer(413, f"a record is at most {MAX_BODY_BYTES} bytes\n")
            try:
                record = decode_body(media_type, self.rfile.read(length))
            except Exception:  # json and cbor2 raise several kinds for a bad body
                return self.answer(400, f"the body is not {media_type}\n")
            if gateway.handle(record) == REFUSED:
                return self.answer(422, "the record does not hold what its mapping needs; see the gateway log\n")
            self.answer(204)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            log.debug("%s %s", self.address_string(), format % args)

    if not is_loopback(host):
        log.warning("listening on %s: plain HTTP on a non-loopback address", host)
    return ThreadingHTTPServer((host, port), Handler)
