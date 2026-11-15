"""SAPIENT edge node: every message carries the fields BSI Flex 335 v2.0
marks mandatory (the `is_mandatory` options in Dstl's proto files), IDs have
the forms the standard gives, and the session runs registration, status,
detections, goodbye against a stand-in fusion node. The run against Dstl's
Apex middleware is tests/e2e/test_apex.py."""

from __future__ import annotations

import re
import socket
import struct
import threading
from typing import Any

import pytest

pytest.importorskip("google.protobuf")

from open_entity_gateway.adapter import DeliveryError
from open_entity_gateway.adapters.sapient import SapientEdgeNode, object_ulid
from open_entity_gateway.gateway import prepare_mappings

from .conftest import MAPPINGS

gen = pytest.importorskip("open_entity_gateway.adapters.sapient._generated.sapient_msg.proto_options_pb2")
from open_entity_gateway.adapters.sapient._generated.sapient_msg.bsi_flex_335_v2_0 import (  # noqa: E402
    sapient_message_pb2,
)

NODE = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
ULID = re.compile("[0-7][0-9A-HJKMNP-TV-Z]{25}")
UUID4 = re.compile("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def missing_mandatory(message: Any, path: str = "") -> list[str]:
    """Every mandatory field or oneof that is absent, as the proto options mark them."""
    missing = []
    for field in message.DESCRIPTOR.fields:
        options = field.GetOptions().Extensions[gen.field_options]
        name = f"{path}{field.name}"
        repeated = field.is_repeated
        if options.is_mandatory:
            present = (
                len(getattr(message, field.name)) > 0
                if repeated
                else (message.HasField(field.name) if field.has_presence else True)
            )
            if not present:
                missing.append(name)
        if options.is_ulid and not repeated and message.HasField(field.name):
            assert ULID.fullmatch(getattr(message, field.name)), name
        if options.is_uuid and not repeated and message.HasField(field.name):
            assert UUID4.fullmatch(getattr(message, field.name)), name
        if field.message_type is not None and not (field.message_type.GetOptions().map_entry):
            values = (
                getattr(message, field.name)
                if repeated
                else ([getattr(message, field.name)] if message.HasField(field.name) else [])
            )
            for i, value in enumerate(values):
                missing += missing_mandatory(value, f"{name}[{i}]." if repeated else f"{name}.")
    for oneof in message.DESCRIPTOR.oneofs:
        if oneof.GetOptions().Extensions[gen.oneof_options].is_mandatory and message.WhichOneof(oneof.name) is None:
            missing.append(f"{path}{oneof.name}")
    return missing


def node(**destination: Any) -> SapientEdgeNode:
    a = SapientEdgeNode({"name": "s", "adapter": "sapient", "node_id": NODE, "address": "127.0.0.1:9", **destination})
    prepare_mappings([MAPPINGS], [a])
    return a


def test_registration_has_every_mandatory_field() -> None:
    message = node().registration()
    assert missing_mandatory(message) == []
    reg = message.registration
    assert reg.icd_version == "BSI Flex 335 v2.0"
    definition = reg.mode_definition[0].detection_definition[0]
    assert [c.type for c in definition.detection_class_definition[0].class_definition] == ["Buoy"]
    assert definition.velocity_type.HasField("enu_velocity_units")


def test_status_reports_have_every_mandatory_field() -> None:
    for new in (True, False):
        assert missing_mandatory(node().status_report(new)) == []


def test_detection_report_for_each_example(ws_position: dict[str, Any], buoy_position: dict[str, Any]) -> None:
    a = SapientEdgeNode({"name": "s", "adapter": "sapient", "node_id": NODE, "address": "127.0.0.1:9"})
    mappings = prepare_mappings([MAPPINGS], [a])
    for record in (ws_position, buoy_position):
        (m,) = [m for m in mappings if m.matches(record)]
        detection = a.encode(m.track(record), record, m.parsed[a.name])
        message = a._message()
        message.detection_report.CopyFrom(detection)
        message.detection_report.report_id = "01JZ0000000000000000000000"
        assert missing_mandatory(message) == []
    buoy = message.detection_report
    assert (buoy.location.y, buoy.location.x, buoy.location.z) == (44.5012345, -30.254321, -1.5)
    assert buoy.id == "Buoy/7"
    assert {i.type: i.value for i in buoy.track_info} == {"capture_time": "2025-06-15T15:06:40Z", "cot_type": "a-f-S"}
    assert buoy.classification[0].type == "Buoy"
    assert buoy.enu_velocity.east_rate == pytest.approx(0.2474873734)


def test_object_id_is_stable_and_a_ulid() -> None:
    assert object_ulid(NODE, "KX7") == object_ulid(NODE, "KX7")
    assert object_ulid(NODE, "KX7") != object_ulid(NODE, "KX8")
    assert ULID.fullmatch(object_ulid(NODE, "KX7"))


def test_node_id_must_be_a_version_4_uuid() -> None:
    from open_entity_gateway.adapter import ConfigError

    with pytest.raises(ConfigError, match="node_id is required"):
        SapientEdgeNode({"name": "s", "adapter": "sapient", "address": "127.0.0.1:9"})
    with pytest.raises(ConfigError, match="node_id"):
        SapientEdgeNode({"name": "s", "adapter": "sapient", "address": "127.0.0.1:9", "node_id": "not-a-uuid"})


class FusionNode:
    """Reads length-prefixed SapientMessages; answers a registration."""

    def __init__(self, accept: bool = True):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.accept = accept
        self.messages: list[Any] = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        connection, _ = self.server.accept()
        buffer = b""
        with connection:
            while chunk := connection.recv(65536):
                buffer += chunk
                while len(buffer) >= 4 and len(buffer) >= 4 + struct.unpack("<I", buffer[:4])[0]:
                    length = struct.unpack("<I", buffer[:4])[0]
                    message = sapient_message_pb2.SapientMessage.FromString(buffer[4 : 4 + length])
                    buffer = buffer[4 + length :]
                    self.messages.append(message)
                    if message.WhichOneof("content") == "registration":
                        ack = sapient_message_pb2.SapientMessage()
                        ack.timestamp.GetCurrentTime()
                        ack.node_id = "00000000-0000-4000-8000-0000000000aa"
                        ack.destination_id = message.node_id
                        ack.registration_ack.acceptance = self.accept
                        body = ack.SerializeToString()
                        connection.sendall(struct.pack("<I", len(body)) + body)


def test_session_against_a_stand_in_fusion_node(ws_position: dict[str, Any]) -> None:
    fusion = FusionNode()
    a = node(address=f"127.0.0.1:{fusion.port}", detection_spacing_ms=0)
    mappings = prepare_mappings([MAPPINGS], [a])
    (m,) = [m for m in mappings if m.matches(ws_position)]
    a.start()
    for _ in range(3):
        a.send(a.encode(m.track(ws_position), ws_position, m.parsed[a.name]))
    a.close()
    fusion.thread.join(5)
    kinds = [x.WhichOneof("content") for x in fusion.messages]
    assert kinds == [
        "registration",
        "status_report",
        "detection_report",
        "detection_report",
        "detection_report",
        "status_report",
    ]
    assert fusion.messages[1].status_report.info == 1  # INFO_NEW
    assert fusion.messages[-1].status_report.system == 5  # SYSTEM_GOODBYE
    reports = [x.detection_report for x in fusion.messages if x.WhichOneof("content") == "detection_report"]
    assert len({r.report_id for r in reports}) == 3 and len({r.object_id for r in reports}) == 1
    for message in fusion.messages:
        assert message.node_id == NODE
        assert missing_mandatory(message) == []


def test_a_refused_registration_sends_no_detection(ws_position: dict[str, Any]) -> None:
    fusion = FusionNode(accept=False)
    a = node(address=f"127.0.0.1:{fusion.port}")
    mappings = prepare_mappings([MAPPINGS], [a])
    (m,) = [m for m in mappings if m.matches(ws_position)]
    with pytest.raises(DeliveryError, match="refused"):
        a.send(a.encode(m.track(ws_position), ws_position, m.parsed[a.name]))
    a.close()
    assert [x.WhichOneof("content") for x in fusion.messages] == ["registration"]
