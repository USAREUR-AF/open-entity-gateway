"""A destination on the goat fabric: each encoded payload published to one key.

The payload is what another adapter (`encode.adapter`: cot, asterix-cat062 or
sapient) writes for the track, one payload per Zenoh sample: a CoT event, an
ASTERIX CAT062 data block, or one SAPIENT message (a DetectionReport) without
the TCP length prefix. This is the form an octo-pod egress destination relays
to a receiver that cannot join the mesh; octo-pod converts no format, and its
`sapient://` destination runs the SAPIENT session itself.

The key is one literal key expression, normally a release key
(`release/<lane>/...`); a wildcard is refused. Whether the gateway may publish
there is decided by the fabric's access control. Endpoints, TLS and
credentials come from a Zenoh configuration file. Needs the `zenoh` extra.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...adapter import Adapter, ConfigError, DeliveryError, available_adapters, require
from ...mapping import Mapping, Track

_KEYS = {"name", "adapter", "topics", "queue", "key", "zenoh_config", "connect", "encode"}
FORMATS = ("cot", "asterix-cat062", "sapient")


class FabricPublisher(Adapter):
    name = "fabric"

    def __init__(self, destination: dict[str, Any]):
        super().__init__(destination)
        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        key = require(destination, "key")
        chunks = key.split("/")
        if any(c == "" or "*" in c or "$" in c for c in chunks):
            raise ConfigError(f"destination {self.label!r}: key {key!r}: one literal key, no wildcards")
        self.key = key
        encode = require(destination, "encode", dict)
        kind = encode.get("adapter")
        if kind not in FORMATS:
            raise ConfigError(f"destination {self.label!r}: encode.adapter is one of {list(FORMATS)}")
        options = {k: v for k, v in encode.items() if k != "adapter"}
        if "print" in options or "address" in options:
            raise ConfigError(f"destination {self.label!r}: encode takes the format's options, not an address")
        self.inner: Adapter = available_adapters()[kind](
            {"name": self.label, "adapter": kind, **options, "print": True}
        )
        config = destination.get("zenoh_config")
        self.zenoh_config = Path(config) if isinstance(config, str) else None
        connect = destination.get("connect", [])
        if not isinstance(connect, list) or not all(isinstance(c, str) for c in connect):
            raise ConfigError(f"destination {self.label!r}: connect: a list of endpoints")
        self.connect: list[str] = connect
        self.session: Any = None
        self.publisher: Any = None

    def block_for(self, mapping: Mapping) -> Any:
        # The mapping's block is the encoding adapter's.
        return mapping.parsed.get(self.inner.name)

    def prepare(self, mappings: list[Mapping]) -> None:
        cls = type(self.inner)
        for mapping in mappings:
            if cls.name not in mapping.parsed:
                mapping.parsed[cls.name] = cls.parse_block(
                    mapping.blocks.get(cls.block) if cls.block else None, mapping
                )
        self.inner.prepare(mappings)

    def encode(self, track: Track, record: dict[str, Any], block: Any) -> bytes:
        payload = self.inner.encode(track, record, block)
        if isinstance(payload, bytes):
            return payload
        message_for = getattr(self.inner, "message_for", None)
        if message_for is not None:  # SAPIENT: the detection report in its SapientMessage
            payload = message_for(payload)
        serialize = getattr(payload, "SerializeToString", None)
        if serialize is None:
            raise TypeError(f"{self.inner.name} wrote a payload that is not bytes")
        return bytes(serialize())

    def start(self) -> None:
        try:
            import zenoh
        except ImportError:
            raise ConfigError(
                "the fabric destination needs eclipse-zenoh: pip install 'open-entity-gateway[zenoh]'"
            ) from None
        conf = zenoh.Config.from_file(str(self.zenoh_config)) if self.zenoh_config else zenoh.Config()
        if self.connect:
            conf.insert_json5("connect/endpoints", json.dumps(self.connect))
        self.session = zenoh.open(conf)
        self.publisher = self.session.declare_publisher(self.key)

    def send(self, payload: Any) -> None:
        try:
            self.publisher.put(payload)
        except Exception as error:  # zenoh raises its own error types
            raise DeliveryError(f"publish to {self.key}: {type(error).__name__}") from None

    def describe(self, payload: Any) -> str:
        text = payload.decode() if self.inner.name == "cot" else payload.hex()
        return f"{self.key} {len(payload)} bytes: {text}"

    def close(self) -> None:
        if self.publisher is not None:
            self.publisher.undeclare()
        if self.session is not None:
            self.session.close()
