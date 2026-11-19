"""Cursor-on-Target: XML events over UDP (unicast or multicast) or TCP,
optionally TLS. See docs/adapters/cot.md."""

from __future__ import annotations

import re
from typing import Any

from ...adapter import Adapter, ConfigError
from ...mapping import Track
from ...record import RecordError, decimal_text
from ...transport import Datagrams, Stream
from .event import ACCESS_VALUES, TIME_FORMS, XML_DECLARATION, escape, event_xml

_KEYS = {
    "name",
    "adapter",
    "topics",
    "queue",
    "transport",
    "address",
    "ttl",
    "interface",
    "source_port",
    "tls",
    "xml_declaration",
    "access",
    "announce",
    "time_form",
    "encoder",
    "print",
}
_ANNOUNCE_KEYS = {"uid", "callsign", "type", "group", "role", "point", "settle_seconds", "interval_seconds"}


# The pattern the MITRE base-event schema gives the `type` attribute.
TYPE_PATTERN = re.compile(r"\w+(-\w+)*(;[^;]*)?")


def _check_text(name: str, text: str) -> None:
    if any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in text):
        raise RecordError(f"the {name} holds a control character, which XML cannot carry")


class CursorOnTarget(Adapter):
    name = "cot"

    def __init__(self, destination: dict[str, Any]):
        super().__init__(destination)
        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        self.printing = bool(destination.get("print"))
        self.encoder = destination.get("encoder", "native")
        if self.encoder not in ("native", "goat-transformers"):
            raise ConfigError(f"destination {self.label!r}: encoder is native or goat-transformers")
        self.access = destination.get("access", "Undefined")
        if self.access not in ACCESS_VALUES:
            raise ConfigError(f"destination {self.label!r}: access is one of {list(ACCESS_VALUES)}")
        self.time_form = destination.get("time_form", "z")
        if self.time_form not in TIME_FORMS:
            raise ConfigError(f"destination {self.label!r}: time_form is one of {sorted(TIME_FORMS)}")
        self.transport_name = destination.get("transport")
        declaration = destination.get("xml_declaration")
        self.transport: Datagrams | Stream | None = None
        if self.printing:
            self.declaration = bool(declaration)
            return
        if self.transport_name == "udp":
            if destination.get("tls") is not None:
                raise ConfigError(f"destination {self.label!r}: tls applies to transport tcp only")
            self.transport = Datagrams(destination, self.label)
            # One event per datagram; TAK clients read it with or without a declaration.
            self.declaration = bool(declaration)
        elif self.transport_name == "tcp":
            self.transport = Stream(destination, self.label)
            self.announce = self._announce_spec(destination.get("announce"))
            if self.announce is not None:
                self.transport.greeting = self.presence
                self.transport.settle = float(self.announce.get("settle_seconds", 1.0))
            # TAK's streaming framing (atak-civ takproto/README.txt) puts an XML
            # declaration and a newline before each event.
            self.declaration = True if declaration is None else bool(declaration)
        else:
            raise ConfigError(f"destination {self.label!r}: transport is udp or tcp")

    announce: dict[str, Any] | None = None
    _announced = 0.0

    def _announce_spec(self, spec: Any) -> dict[str, Any] | None:
        if spec is None:
            return None
        if not isinstance(spec, dict) or "uid" not in spec or set(spec) - _ANNOUNCE_KEYS:
            raise ConfigError(
                f"destination {self.label!r}: announce takes uid and optionally {sorted(_ANNOUNCE_KEYS - {'uid'})}"
            )
        point = spec.get("point", [0, 0, 0])
        if not (isinstance(point, list) and len(point) == 3 and all(isinstance(v, (int, float)) for v in point)):
            raise ConfigError(f"destination {self.label!r}: announce.point is [latitude, longitude, hae]")
        return dict(spec)

    def presence(self) -> bytes:
        """The gateway's own presence event, sent first on each connection
        and again every `interval_seconds`: a TAK server routes the events of
        a connection that has announced itself (measured with OpenTAKServer)."""
        import time as clock

        assert self.announce is not None
        spec = self.announce
        now = clock.time_ns()
        write = TIME_FORMS[self.time_form]
        time = write(now)
        stale = write(now + int(float(spec.get("interval_seconds", 60)) * 3 * 1e9))
        uid = str(spec["uid"])
        lat, lon, hae = (decimal_text(float(v)) for v in spec.get("point", [0, 0, 0]))
        self._announced = clock.monotonic()
        xml = (
            f'<event version="2.0" uid="{escape(uid)}" type="{escape(str(spec.get("type", "a-f-G-U-C")))}"'
            f' time="{time}" start="{time}" stale="{stale}" how="h-g-i-g-o" access="{escape(self.access)}">'
            f'<point lat="{lat}" lon="{lon}" hae="{hae}" ce="9999999" le="9999999"/><detail>'
            f'<contact callsign="{escape(str(spec.get("callsign", uid)))}"/>'
            f'<__group name="{escape(str(spec.get("group", "Cyan")))}"'
            f' role="{escape(str(spec.get("role", "Team Member")))}"/>'
            f'<takv device="open-entity-gateway" platform="open-entity-gateway" os="" version=""/>'
            "</detail></event>"
        )
        return (XML_DECLARATION + xml).encode()

    def encode(self, track: Track, record: dict[str, Any], block: Any) -> bytes:
        _check_text("uid", track.uid)
        _check_text("type", track.cot_type)
        if not TYPE_PATTERN.fullmatch(track.cot_type):
            raise RecordError(f"the CoT type {track.cot_type!r} does not have the form the CoT schema gives a type")
        if track.name is not None:
            _check_text("name", track.name)
        if self.encoder == "goat-transformers":
            from ...library import cot

            return cot(track)
        xml = event_xml(track, self.access, self.time_form)
        return ((XML_DECLARATION if self.declaration else "") + xml).encode("utf-8")

    def start(self) -> None:
        if isinstance(self.transport, Datagrams):
            self.transport.open()

    def send(self, payload: bytes) -> None:
        assert self.transport is not None
        if self.announce is not None and isinstance(self.transport, Stream) and self.transport.sock is not None:
            import time as clock

            if clock.monotonic() - self._announced > float(self.announce.get("interval_seconds", 60)):
                self.transport.send(self.presence())
        self.transport.send(payload)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
