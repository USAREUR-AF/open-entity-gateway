"""SAPIENT to Dstl's Apex middleware, end to end.

The gateway connects to Apex's Child port (BSI Flex 335 v2.0, protobuf) as an
edge node: registration, status, a detection report for each example track,
goodbye. A stand-in decision-making module connects to Apex's Peer port and
receives what Apex forwards. The test passes when Apex accepts the
registration, reports no error to the node (Apex answers every message that
fails its validation with an Error message), and forwards the detections.

Runs when OEG_E2E_APEX (the Apex host) is set; tests/e2e/run-on-build-host.sh
sets it. Apex's validation options are its defaults (tests/e2e/apex/apex_config.json).
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from typing import Any

import pytest

from open_entity_gateway.adapters.sapient import SapientEdgeNode
from open_entity_gateway.gateway import Destination, Gateway, prepare_mappings
from open_entity_gateway.inputs.replay import shift_to

from ..conftest import MAPPINGS

APEX = os.environ.get("OEG_E2E_APEX")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not APEX, reason="OEG_E2E_APEX is not set")]
NODE = "7c6f3a52-1e0b-4f7d-8a39-5b2e9d4c1f60"
DMM = "c0ffee00-1234-4abc-8def-0123456789ab"


class DecisionModule:
    """Connects to Apex's Peer port and keeps every SapientMessage it gets."""

    def __init__(self, host: str, port: int):
        from open_entity_gateway.adapters.sapient._generated.sapient_msg.bsi_flex_335_v2_0 import (
            sapient_message_pb2,
        )

        self.pb = sapient_message_pb2
        self.sock = socket.create_connection((host, port), timeout=10)
        self.messages: list[Any] = []
        self.stop = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        buffer = b""
        self.sock.settimeout(0.5)
        while not self.stop.is_set():
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                return
            buffer += chunk
            while len(buffer) >= 4 and len(buffer) >= 4 + struct.unpack("<I", buffer[:4])[0]:
                length = struct.unpack("<I", buffer[:4])[0]
                self.messages.append(self.pb.SapientMessage.FromString(buffer[4 : 4 + length]))
                buffer = buffer[4 + length :]

    def close(self) -> None:
        self.stop.set()
        self.sock.close()


def test_edge_node_to_apex(ws_position: dict[str, Any], buoy_position: dict[str, Any]) -> None:
    assert APEX
    dmm = DecisionModule(APEX, 5001)
    adapter = SapientEdgeNode(
        {
            "name": "apex",
            "adapter": "sapient",
            "address": f"{APEX}:5020",
            "node_id": NODE,
            "status_interval_seconds": 2,
        }
    )
    mappings = prepare_mappings([MAPPINGS], [adapter])
    gateway = Gateway(mappings, [Destination(adapter, None, 10)])
    gateway.start()
    records = list(shift_to(iter([ws_position, buoy_position, ws_position]), time.time_ns()))
    for record in records:
        assert gateway.handle(record) == "sent"
    time.sleep(5)  # two status intervals
    gateway.close()
    time.sleep(1)
    dmm.close()

    assert adapter.acceptance is True, "Apex did not accept the registration"
    assert adapter.errors == [], f"Apex reported errors: {adapter.errors}"
    assert gateway.destinations[0].counts.sent == 3

    from_node = [m for m in dmm.messages if m.node_id == NODE]
    kinds = [m.WhichOneof("content") for m in from_node]
    print("forwarded by Apex to the decision module:", kinds)
    detections = [m.detection_report for m in from_node if m.WhichOneof("content") == "detection_report"]
    assert len(detections) == 3
    assert "registration" in kinds and "status_report" in kinds
    weather, buoy = detections[0], detections[1]
    assert (weather.location.y, weather.location.x, weather.location.z) == (64.8378, -147.7164, 136.5)
    assert weather.id == "KX7" and buoy.id == "Buoy/7"
    assert buoy.classification[0].type == "Buoy"
    assert detections[0].object_id == detections[2].object_id != buoy.object_id
