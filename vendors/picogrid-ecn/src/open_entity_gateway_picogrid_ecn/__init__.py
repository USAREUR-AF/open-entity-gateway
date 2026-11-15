"""Picogrid Expeditionary C2 Node: each track is published as an entity with
its position, through Picogrid's ECN SDK (`picogrid-ecn-client`, MPL-2.0).

The SDK connects over MQTT v5 and owns authentication: the destination names
an SDK profile, or the SDK reads its own `ECN_*` environment variables. No
credential is in the gateway's configuration. See docs/adapters/picogrid-ecn.md.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import threading
import uuid
from typing import Any

from open_entity_gateway.adapter import Adapter, ConfigError, DeliveryError
from open_entity_gateway.mapping import Mapping, MappingError, Track, Value
from open_entity_gateway.record import NANOS

# The categories the SDK accepts for publication (`OTHER` is decode-only).
CATEGORIES = (
    "DEVICE",
    "DETECTION",
    "TRACK",
    "SYSTEM",
    "SENSOR",
    "ALERT",
    "WEATHER",
    "GEOMETRIC",
    "ZONE",
    "VEHICLE",
    "UXV",
)
# CoT affiliation (open_entity_gateway.mapping.AFFILIATIONS) to the SDK's Affiliation.
AFFILIATION = {
    "friend": "FRIEND",
    "hostile": "HOSTILE",
    "neutral": "NEUTRAL",
    "unknown": "UNKNOWN",
    "suspect": "SUSPECT",
    "pending": "PENDING",
    "assumed-friend": "ASSUMED_FRIEND",
    "joker": "JOKER",
    "faker": "FAKER",
    "none": "NONE_SPECIFIED",
    "other": "UNKNOWN",
}
# Entity ids are UUIDs: version 5 in this namespace, of the integration and the track uid.
NAMESPACE = uuid.UUID("6f0d3a0e-8c1b-5d2a-9e4f-1a2b3c4d5e6f")
_KEYS = {"name", "adapter", "topics", "queue", "profile", "timeout_seconds", "print"}
_BLOCK_KEYS = {"category", "type", "status"}


def entity_uuid(integration: str, uid: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{integration}\0{uid}")


class Block:
    """Without a block: category TRACK, type the track's CoT type, status ACTIVE."""

    def __init__(self, category: str = "TRACK", entity_type: Value | None = None, status: str = "ACTIVE"):
        self.category = category
        self.entity_type = entity_type
        self.status = status


class PicogridECN(Adapter):
    name = "picogrid-ecn"
    block = "picogrid_ecn"

    def __init__(self, destination: dict[str, Any], config: Any = None):
        super().__init__(destination)
        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        try:
            import picogrid_ecn_client as ecn
        except ImportError:
            raise ConfigError(
                "the picogrid-ecn adapter needs Picogrid's ECN SDK: pip install open-entity-gateway-picogrid-ecn"
            ) from None
        self.ecn = ecn
        self.printing = bool(destination.get("print"))
        self.timeout = float(destination.get("timeout_seconds", 10))
        if config is not None:
            self.config = config
        elif self.printing:
            self.config = None
        else:
            try:
                self.config = ecn.load_config(profile=destination.get("profile"))
            except ecn.ConfigurationError as error:
                raise ConfigError(
                    f"destination {self.label!r}: the ECN SDK has no usable configuration: {error}"
                ) from None
        self.integration = self.config.integration_name if self.config is not None else "print"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self.receipts: list[Any] = []

    @classmethod
    def parse_block(cls, block: Any, mapping: Mapping) -> Block:
        where = f"{mapping.name}: picogrid_ecn"
        if block is None:
            return Block()
        if not isinstance(block, dict) or set(block) - _BLOCK_KEYS:
            raise MappingError(f"{where}: takes {sorted(_BLOCK_KEYS)}")
        category = block.get("category", "TRACK")
        if category not in CATEGORIES:
            raise MappingError(f"{where}.category: one of {list(CATEGORIES)}")
        status = block.get("status", "ACTIVE")
        if status not in ("ACTIVE", "INACTIVE", "UNKNOWN"):
            raise MappingError(f"{where}.status: ACTIVE, INACTIVE or UNKNOWN")
        entity_type = Value(block["type"], f"{where}.type") if "type" in block else None
        return Block(category, entity_type, status)

    def encode(self, track: Track, record: dict[str, Any], block: Block | None) -> Any:
        from open_entity_gateway.record import RecordError

        block = block or Block()
        ecn = self.ecn
        recorded = dt.datetime.fromtimestamp(track.time_ns // NANOS, dt.UTC) + dt.timedelta(
            microseconds=(track.time_ns % NANOS) // 1000
        )
        entity_type = block.entity_type.resolve(record) if block.entity_type else track.cot_type
        if not 1 <= len(entity_type) <= 128:
            raise RecordError("picogrid_ecn.type must be 1 to 128 characters")
        velocity = None
        if track.course is not None and track.speed is not None:
            course = math.radians(track.course)
            velocity = ecn.Velocity(north=track.speed * math.cos(course), east=track.speed * math.sin(course), down=0.0)
        try:
            location = ecn.Location(
                latitude=track.latitude,
                longitude=track.longitude,
                recorded_at=recorded,
                altitude=track.hae,
                bearing=track.course,
                velocity=velocity,
            )
            return ecn.Entity(
                id=entity_uuid(self.integration, track.uid),
                category=ecn.EntityCategory(block.category),
                integration=self.integration,
                recorded_at=recorded,
                type=entity_type,
                name=track.name[:256] if track.name else None,
                status=ecn.EntityStatus(block.status),
                affiliation=ecn.Affiliation(AFFILIATION.get(track.affiliation, "UNKNOWN")),
                position=location,
            )
        except ValueError as error:  # pydantic's ValidationError is a ValueError
            raise RecordError(f"the ECN SDK does not accept the entity: {error.__class__.__name__}") from None

    def describe(self, payload: Any) -> str:
        return str(payload.model_dump_json(exclude_none=True))

    # -- the SDK is asynchronous: it runs on its own event loop and thread ------
    def start(self) -> None:
        if self.printing:
            return
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name=f"ecn-{self.label}").start()
        self._client = self.ecn.ECNClient(self.config)
        self._run(self._client.start())

    def _run(self, coroutine: Any) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(self.timeout)
        except TimeoutError:
            future.cancel()
            raise DeliveryError(f"the ECN SDK did not finish within {self.timeout:.0f} s") from None
        except self.ecn.ECNClientError as error:
            raise DeliveryError(f"the ECN SDK refused: {error.__class__.__name__}") from None

    def send(self, payload: Any) -> None:
        assert self._client is not None
        if not self._client.is_ready:
            self._run(self._client.wait_until_ready(timeout=self.timeout))
        self.receipts.append(self._run(self._client.entities.publish(payload)))

    def close(self) -> None:
        if self._client is not None and self._loop is not None:
            try:
                self._run(self._client.close())
            except DeliveryError:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
