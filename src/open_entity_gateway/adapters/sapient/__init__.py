"""SAPIENT (BSI Flex 335 v2.0): the gateway as an edge node.

On connecting, the node sends a Registration and waits for the
RegistrationAck; it then sends a StatusReport (INFO_NEW) and one every
`status_interval_seconds` after (INFO_UNCHANGED); each track is a
DetectionReport. Messages are length-prefixed protobuf: a 4-byte
little-endian length, then the SapientMessage, the framing Dstl's Apex
middleware reads. See docs/adapters/sapient.md.

The bindings are generated from Dstl's proto files (Apache-2.0) by
tools/fetch-sapient-proto.py.

With `encoder: goat-transformers` each DetectionReport is written by the
goat-transformers pack `sapient` (docs/goat-transformers-encoder.md); the
session, the message timestamp, node id and report id stay the gateway's.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import struct
import threading
import time
from typing import Any

from ...adapter import Adapter, ConfigError, DeliveryError
from ...mapping import Mapping, MappingError, Track, Value
from ...record import RecordError, rfc3339_z
from ...transport import Stream

log = logging.getLogger("open_entity_gateway")

ICD_VERSION = "BSI Flex 335 v2.0"
NODE_TYPES = (
    "OTHER",
    "RADAR",
    "LIDAR",
    "CAMERA",
    "SEISMIC",
    "ACOUSTIC",
    "PROXIMITY_SENSOR",
    "PASSIVE_RF",
    "HUMAN",
    "CHEMICAL",
    "BIOLOGICAL",
    "RADIATION",
    "FUSION_NODE",
)
_KEYS = {
    "name",
    "adapter",
    "topics",
    "queue",
    "address",
    "tls",
    "node_id",
    "node_name",
    "short_name",
    "node_type",
    "manufacturer",
    "model",
    "status_interval_seconds",
    "detection_spacing_ms",
    "registration_timeout_seconds",
    "print",
    "encoder",
}
_BLOCK_KEYS = {"classification"}
# A documentation UUID for `print`, which sends nothing.
PRINT_NODE_ID = "00000000-0000-4000-8000-000000000000"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_UUID4 = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


def _bindings() -> Any:
    try:
        from ._generated.sapient_msg.bsi_flex_335_v2_0 import (
            detection_report_pb2,
            location_pb2,
            registration_pb2,
            sapient_message_pb2,
            status_report_pb2,
            velocity_pb2,
        )
    except ImportError as error:
        raise ConfigError(
            "the SAPIENT adapter needs its bindings and the protobuf runtime: "
            "pip install 'open-entity-gateway[sapient]' and run tools/fetch-sapient-proto.py "
            f"({error})"
        ) from None
    return type(
        "B",
        (),
        {
            "detection": detection_report_pb2,
            "location": location_pb2,
            "registration": registration_pb2,
            "message": sapient_message_pb2,
            "status": status_report_pb2,
            "velocity": velocity_pb2,
        },
    )


def ulid(value: int) -> str:
    """A 128-bit integer as a ULID: 26 Crockford base-32 characters."""
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -5, -5))


def new_ulid(now_ns: int | None = None) -> str:
    millis = (now_ns if now_ns is not None else time.time_ns()) // 1_000_000
    return ulid((millis & (2**48 - 1)) << 80 | int.from_bytes(os.urandom(10), "big"))


def object_ulid(node_id: str, uid: str) -> str:
    """A stable object_id: SHA-256 of the node and the track uid, written as a
    ULID. One object keeps its object_id across restarts; its time part
    carries no time."""
    return ulid(int.from_bytes(hashlib.sha256(f"{node_id}\0{uid}".encode()).digest()[:16], "big"))


def frame(message: Any) -> bytes:
    body: bytes = message.SerializeToString()
    return struct.pack("<I", len(body)) + body


class Block:
    def __init__(self, classification: Value | None, classes: set[str]):
        self.classification = classification
        self.classes = classes


class SapientEdgeNode(Adapter):
    name = "sapient"
    block = "sapient"

    def __init__(self, destination: dict[str, Any]):
        super().__init__(destination)
        import re

        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        self.printing = bool(destination.get("print"))
        node_id = destination.get("node_id", PRINT_NODE_ID if self.printing else None)
        if not isinstance(node_id, str) or not re.fullmatch(_UUID4, node_id):
            raise ConfigError(
                f"destination {self.label!r}: node_id is required: this node's UUID (version 4, lower case), "
                "chosen once and kept"
            )
        self.node_id = node_id
        node_type = destination.get("node_type", "OTHER")
        if node_type not in NODE_TYPES:
            raise ConfigError(f"destination {self.label!r}: node_type is one of {list(NODE_TYPES)}")
        self.node_type = f"NODE_TYPE_{node_type}"
        self.node_name = str(destination.get("node_name", "open-entity-gateway"))
        self.short_name = str(destination.get("short_name", "gateway"))
        from ... import __version__

        self.manufacturer = str(destination.get("manufacturer", "open-entity-gateway"))
        self.model = str(destination.get("model", f"open-entity-gateway {__version__}"))
        self.status_interval = float(destination.get("status_interval_seconds", 10))
        self.spacing = float(destination.get("detection_spacing_ms", 100)) / 1000
        self.ack_timeout = float(destination.get("registration_timeout_seconds", 10))
        self.b = _bindings()
        from ...library import choose

        self.encoder = choose(destination, self.label)
        self.stream = None if self.printing else Stream(destination, self.label)
        self.classes: set[str] = set()
        self.with_velocity = False
        self._write = threading.Lock()
        self._session = threading.Event()
        self._acked = threading.Event()
        self._stop = threading.Event()
        self._last_detection = 0.0
        self.errors: list[str] = []
        self.acceptance: bool | None = None

    # -- mapping ------------------------------------------------------------
    @classmethod
    def parse_block(cls, block: Any, mapping: Mapping) -> Block:
        where = f"{mapping.name}: sapient"
        if block is None:
            return Block(None, set())
        if not isinstance(block, dict) or set(block) - _BLOCK_KEYS:
            raise MappingError(f"{where}: takes {sorted(_BLOCK_KEYS)}")
        classification = (
            Value(block["classification"], f"{where}.classification") if "classification" in block else None
        )
        classes: set[str] = set()
        if classification is not None:
            results = classification.results()
            if results is None:
                raise MappingError(
                    f"{where}.classification: a constant or a lookup with values, so the registration "
                    "can declare every class"
                )
            classes = results
        return Block(classification, classes)

    def prepare(self, mappings: list[Mapping]) -> None:
        """Called once the mappings are loaded: the registration declares
        every class and whether velocity is reported."""
        for mapping in mappings:
            parsed = mapping.parsed.get(self.name)
            if isinstance(parsed, Block):
                self.classes |= parsed.classes
            if mapping.course_field:
                self.with_velocity = True

    # -- messages -------------------------------------------------------------
    def _message(self, now_ns: int | None = None) -> Any:
        message = self.b.message.SapientMessage()
        message.timestamp.FromNanoseconds(now_ns if now_ns is not None else time.time_ns())
        message.node_id = self.node_id
        return message

    def registration(self) -> Any:
        r = self.b.registration.Registration
        message = self._message()
        reg = message.registration
        reg.node_definition.add().node_type = getattr(r, self.node_type)
        reg.icd_version = ICD_VERSION
        reg.name = self.node_name
        reg.short_name = self.short_name
        capability = reg.capabilities.add()
        capability.category = "Gateway"
        capability.type = "Tracks from decoded first-bite records"
        reg.status_definition.status_interval.units = r.TIME_UNITS_SECONDS
        reg.status_definition.status_interval.value = self.status_interval
        mode = reg.mode_definition.add()
        mode.mode_name = "default"
        mode.mode_type = r.MODE_TYPE_PERMANENT
        mode.settle_time.units = r.TIME_UNITS_SECONDS
        mode.settle_time.value = 0
        mode.tracking_type = r.TRACKING_TYPE_TRACK
        definition = mode.detection_definition.add()
        definition.location_type.location_units = self.b.location.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
        definition.location_type.location_datum = self.b.location.LOCATION_DATUM_WGS84_E
        for kind, units in (("capture_time", "RFC 3339 UTC"), ("cot_type", "Cursor-on-Target type")):
            entry = definition.detection_report.add()
            entry.category = r.DETECTION_REPORT_CATEGORY_TRACK
            entry.type = kind
            entry.units = units
        if self.classes:
            classes = definition.detection_class_definition.add()
            classes.confidence_definition = r.CONFIDENCE_DEFINITION_SINGLE_CLASS
            for name in sorted(self.classes):
                classes.class_definition.add().type = name
        if self.with_velocity:
            definition.velocity_type.enu_velocity_units.east_north_rate_units = self.b.velocity.SPEED_UNITS_MS
            definition.velocity_type.location_datum = self.b.location.LOCATION_DATUM_WGS84_E
        region = mode.task.region_definition
        region.region_type.append(r.REGION_TYPE_BOUNDARY)
        area = region.region_area.add()
        area.location_units = self.b.location.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
        area.location_datum = self.b.location.LOCATION_DATUM_WGS84_E
        config = reg.config_data.add()
        config.manufacturer = self.manufacturer
        config.model = self.model
        return message

    def status_report(self, new: bool) -> Any:
        s = self.b.status.StatusReport
        message = self._message()
        status = message.status_report
        status.report_id = new_ulid()
        status.system = s.SYSTEM_OK
        status.info = s.INFO_NEW if new else s.INFO_UNCHANGED
        status.mode = "default"
        return message

    def encode(self, track: Track, record: dict[str, Any], block: Block | None) -> Any:
        if self.encoder == "goat-transformers":
            from ...library import sapient

            classification = (
                block.classification.resolve(record) if block is not None and block.classification is not None else None
            )
            written = sapient(track, object_ulid(self.node_id, track.uid), track.name, classification)
            message = self.b.message.SapientMessage.FromString(written)
            if message.WhichOneof("content") != "detection_report":
                raise RecordError(f"goat-transformers wrote a SAPIENT {message.WhichOneof('content')}, not a detection")
            return message.detection_report
        detection = self.b.detection.DetectionReport()
        detection.object_id = object_ulid(self.node_id, track.uid)
        location = detection.location
        location.x = track.longitude
        location.y = track.latitude
        if track.hae is not None:
            location.z = track.hae
        location.coordinate_system = self.b.location.LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M
        location.datum = self.b.location.LOCATION_DATUM_WGS84_E
        if track.name is not None:
            detection.id = track.name
        for kind, value in (("capture_time", rfc3339_z(track.time_ns)), ("cot_type", track.cot_type)):
            info = detection.track_info.add()
            info.type = kind
            info.value = value
        if block is not None and block.classification is not None:
            detection.classification.add().type = block.classification.resolve(record)
        if track.course is not None and track.speed is not None:
            import math

            course = math.radians(track.course)
            detection.enu_velocity.east_rate = track.speed * math.sin(course)
            detection.enu_velocity.north_rate = track.speed * math.cos(course)
        return detection

    def message_for(self, detection: Any) -> Any:
        """The SapientMessage that carries one detection report, with a new
        report id: what is sent, and what the fabric destination publishes."""
        message = self._message()
        message.detection_report.CopyFrom(detection)
        message.detection_report.report_id = new_ulid()
        return message

    def describe(self, payload: Any) -> str:
        from google.protobuf.json_format import MessageToJson

        message = self.message_for(payload)
        return str(MessageToJson(message, indent=None, preserving_proto_field_name=True))

    # -- session ----------------------------------------------------------------
    def _send(self, message: Any) -> None:
        assert self.stream is not None
        with self._write:
            sock = self.stream.sock
            if sock is None:
                raise DeliveryError("not connected")
            try:
                sock.sendall(frame(message))
            except OSError as error:
                self._drop()
                raise DeliveryError(f"connection lost: {type(error).__name__}") from None

    def _drop(self) -> None:
        assert self.stream is not None
        self._session.clear()
        self._acked.clear()
        self.stream._drop()

    def _read(self, sock: socket.socket) -> None:
        buffer = b""
        sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= 4:
                (length,) = struct.unpack("<I", buffer[:4])
                if len(buffer) < 4 + length:
                    break
                body, buffer = buffer[4 : 4 + length], buffer[4 + length :]
                message = self.b.message.SapientMessage()
                try:
                    message.ParseFromString(body)
                except Exception:  # protobuf raises DecodeError; name it, keep reading
                    log.warning("destination %s: a message that is not a SapientMessage", self.label)
                    continue
                self._received(message)
        self._session.clear()
        self._acked.clear()

    def _received(self, message: Any) -> None:
        kind = message.WhichOneof("content")
        if kind == "registration_ack":
            self.acceptance = message.registration_ack.acceptance
            reasons = list(message.registration_ack.ack_response_reason)
            if self.acceptance:
                log.info("destination %s: registration accepted", self.label)
            else:
                log.error("destination %s: registration refused: %s", self.label, reasons)
            self._acked.set()
        elif kind == "error":
            text = "; ".join(message.error.error_message)
            self.errors.append(text)
            log.error("destination %s: the fusion node reports an error: %s", self.label, text)
        elif kind is not None:
            log.info("destination %s: received %s, which this node does not act on", self.label, kind)

    def _open_session(self) -> None:
        assert self.stream is not None
        if self.stream.sock is None:
            self._session.clear()
            self.stream.sock = self.stream.connect()
            self._acked.clear()
            threading.Thread(
                target=self._read, args=(self.stream.sock,), daemon=True, name=f"sapient-read-{self.label}"
            ).start()
        if self._session.is_set():
            return
        self._send(self.registration())
        if not self._acked.wait(self.ack_timeout):
            self._drop()
            raise DeliveryError(f"no registration acknowledgement within {self.ack_timeout:.0f} s")
        if not self.acceptance:
            raise DeliveryError("registration refused by the fusion node")
        self._send(self.status_report(new=True))
        self._session.set()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.status_interval):
            if self._session.is_set():
                try:
                    self._send(self.status_report(new=False))
                except DeliveryError as error:
                    log.warning("destination %s: status report not sent: %s", self.label, error)

    def start(self) -> None:
        if self.stream is not None:
            threading.Thread(target=self._heartbeat, daemon=True, name=f"sapient-status-{self.label}").start()

    def send(self, payload: Any) -> None:
        for attempt in (1, 2):
            self._open_session()
            wait = self._last_detection + self.spacing - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            message = self._message()
            message.detection_report.CopyFrom(payload)
            message.detection_report.report_id = new_ulid()
            try:
                self._send(message)
            except DeliveryError:
                if attempt == 2:
                    raise
                continue
            self._last_detection = time.monotonic()
            return

    def close(self) -> None:
        self._stop.set()
        if self.stream is not None:
            if self._session.is_set():
                # A change of system state is new information (Apex refuses an INFO_UNCHANGED goodbye).
                goodbye = self.status_report(new=True)
                goodbye.status_report.system = self.b.status.StatusReport.SYSTEM_GOODBYE
                try:
                    self._send(goodbye)
                except DeliveryError:
                    pass
            time.sleep(0.2)
            self.stream.close()
