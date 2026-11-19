"""The Zenoh input: named key expressions only, and records and Canonical
Track v2 objects published on the fabric reach the destinations."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest

from open_entity_gateway.cli import main
from open_entity_gateway.gateway import SENT, Destination, Gateway, prepare_mappings
from open_entity_gateway.inputs.zenoh_input import KeyExpressionError, check_keys

from .conftest import MAPPINGS, ROOT
from .test_gateway import Recorder


@pytest.mark.parametrize("key", ["**", "*/**", "**/*", "*"])
def test_a_key_of_wildcards_only_is_refused(key: str) -> None:
    with pytest.raises(KeyExpressionError, match="whole keyspace"):
        check_keys([key])


@pytest.mark.parametrize("keys", [[], ["tracks//x"], [""]])
def test_no_key_or_a_malformed_key_is_refused(keys: list[str]) -> None:
    with pytest.raises(KeyExpressionError):
        check_keys(keys)


def test_named_keys_with_wildcards_are_accepted() -> None:
    assert check_keys(["sensors/*/tracks", "partner/**"]) == ["sensors/*/tracks", "partner/**"]


def test_the_command_refuses_the_whole_keyspace(tmp_path: Any, capsys: Any) -> None:
    config = tmp_path / "gateway.yml"
    config.write_text(f"mappings: [{MAPPINGS}]\ndestinations: [{{name: p, adapter: cot, print: true}}]\n")
    assert main(["zenoh", "--config", str(config), "--key", "**"]) == 2
    assert "whole keyspace" in capsys.readouterr().err


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def peer(listen: str | None = None, connect: str | None = None) -> Any:
    import zenoh

    conf = zenoh.Config()
    conf.insert_json5("mode", '"peer"')
    conf.insert_json5("scouting/multicast/enabled", "false")
    if listen:
        conf.insert_json5("listen/endpoints", json.dumps([listen]))
    if connect:
        conf.insert_json5("connect/endpoints", json.dumps([connect]))
    return zenoh.open(conf)


def test_records_and_tracks_on_the_fabric_reach_the_destinations(buoy_position: dict[str, Any]) -> None:
    pytest.importorskip("zenoh")
    from open_entity_gateway.inputs.zenoh_input import records

    endpoint = f"tcp/127.0.0.1:{free_port()}"
    reader, writer = peer(listen=endpoint), peer(connect=endpoint)
    recorder = Recorder({"name": "r", "adapter": "recorder"})
    gateway = Gateway(prepare_mappings([MAPPINGS], [recorder]), [Destination(recorder, None, 10)])
    gateway.start()
    track = json.loads((ROOT / "examples" / "tracks" / "example.tracks.jsonl").read_text().splitlines()[0])
    outcomes: list[str] = []

    def run() -> None:
        for value in records(["test/records", "test/tracks"], session=reader):
            outcomes.append(gateway.handle(value))
            if len(outcomes) == 2:
                return

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.5)  # the subscribers are declared
    deadline = time.monotonic() + 10
    while len(outcomes) < 2 and time.monotonic() < deadline:
        if not outcomes:
            writer.put("test/records", json.dumps(buoy_position).encode())
        writer.put("test/tracks", json.dumps(track).encode())
        writer.put("other/key", json.dumps(track).encode())
        thread.join(0.5)
    gateway.close()
    writer.close()
    reader.close()
    assert outcomes[:2] == [SENT, SENT]
    assert {"example-radar-1_T-0007"} <= set(recorder.sent)
