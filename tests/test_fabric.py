"""The fabric destination: one literal key, the payload another adapter
writes, one payload per sample, as an octo-pod egress destination relays it."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from open_entity_gateway.adapter import ConfigError
from open_entity_gateway.adapters.cot import CursorOnTarget
from open_entity_gateway.adapters.fabric import FabricPublisher
from open_entity_gateway.gateway import SENT, Destination, Gateway, prepare_mappings

from .conftest import MAPPINGS, ROOT
from .test_zenoh_input import free_port, peer

TRACK = json.loads((ROOT / "examples" / "tracks" / "example.tracks.jsonl").read_text().splitlines()[0])


def fabric(**extra: Any) -> FabricPublisher:
    spec: dict[str, Any] = {
        "name": "f",
        "adapter": "fabric",
        "key": "release/lane-a/tracks/cot",
        "encode": {"adapter": "cot"},
    }
    return FabricPublisher({**spec, **extra})


@pytest.mark.parametrize("key", ["release/*/cot", "release/**", "release//cot", "release/$*x"])
def test_the_key_is_one_literal_key(key: str) -> None:
    with pytest.raises(ConfigError, match="literal"):
        fabric(key=key)


@pytest.mark.parametrize(
    ("encode", "message"),
    [({"adapter": "lattice"}, "encode.adapter"), ({"adapter": "cot", "address": "x:1"}, "not an address")],
)
def test_encode_names_an_open_standard_adapter_and_no_address(encode: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        fabric(encode=encode)


def payload_of(adapter: Any) -> bytes:
    from open_entity_gateway.canonical_track import track_record

    mappings = prepare_mappings([MAPPINGS], [adapter])
    record = track_record(TRACK)
    (m,) = [m for m in mappings if m.matches(record)]
    return bytes(adapter.encode(m.track(record, received_ns=0), record, adapter.block_for(m)))


def test_the_payload_is_what_the_encoding_adapter_writes() -> None:
    cot = CursorOnTarget({"name": "c", "adapter": "cot", "print": True})
    assert payload_of(fabric()) == payload_of(cot)


def test_sapient_is_one_bare_message_per_sample() -> None:
    pytest.importorskip("google.protobuf")
    gen = pytest.importorskip(
        "open_entity_gateway.adapters.sapient._generated.sapient_msg.bsi_flex_335_v2_0.sapient_message_pb2"
    )
    node = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
    payload = payload_of(fabric(key="release/lane-a/tracks/sapient", encode={"adapter": "sapient", "node_id": node}))
    message = gen.SapientMessage()
    message.ParseFromString(payload)
    assert message.HasField("detection_report") and message.node_id == node


def test_a_track_is_published_on_the_key(tmp_path: Path) -> None:
    pytest.importorskip("zenoh")
    endpoint = f"tcp/127.0.0.1:{free_port()}"
    reader = peer(listen=endpoint)
    config = tmp_path / "fabric.json5"
    config.write_text(
        json.dumps(
            {"mode": "peer", "scouting": {"multicast": {"enabled": False}}, "connect": {"endpoints": [endpoint]}}
        )
    )
    got: list[bytes] = []
    arrived = threading.Event()

    def take(sample: Any) -> None:
        got.append(sample.payload.to_bytes())
        arrived.set()

    subscriber = reader.declare_subscriber("release/lane-a/tracks/cot", take)
    adapter = fabric(zenoh_config=str(config))
    gateway = Gateway(prepare_mappings([MAPPINGS], [adapter]), [Destination(adapter, None, 10)])
    gateway.start()
    time.sleep(0.5)
    deadline = time.monotonic() + 10
    while not arrived.is_set() and time.monotonic() < deadline:
        assert gateway.handle(TRACK) == SENT
        arrived.wait(0.5)
    gateway.close()
    subscriber.undeclare()
    reader.close()
    assert got and got[0].startswith(b"<event") and b'uid="example-radar-1_T-0007"' in got[0]
