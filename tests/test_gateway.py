"""The core: no default destination, refusal sends nothing anywhere, the
inputs (rest listener, replay), and the configuration."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

import cbor2
import pytest

from open_entity_gateway.adapter import Adapter, ConfigError
from open_entity_gateway.cli import main
from open_entity_gateway.gateway import REFUSED, SENT, SKIPPED, Destination, Gateway, gateway_from_config
from open_entity_gateway.inputs.http import make_server
from open_entity_gateway.inputs.replay import shift_to
from open_entity_gateway.mapping import Mapping, Track, load_mappings
from open_entity_gateway.record import RecordError, field_text

from .conftest import MAPPINGS, RECORDS


class Recorder(Adapter):
    """A destination that keeps what it is sent. `needs` is a message field
    its encode reads, to make one destination refuse what another accepts."""

    name = "recorder"

    def __init__(self, destination: dict[str, Any]):
        super().__init__(destination)
        self.needs = destination.get("needs")
        self.sent: list[Any] = []
        self.event = threading.Event()

    def encode(self, track: Track, record: dict[str, Any], block: Any) -> Any:
        if self.needs:
            field_text(record, self.needs)
        return track.uid

    def send(self, payload: Any) -> None:
        self.sent.append(payload)
        self.event.set()


def gateway(*destinations: Recorder) -> Gateway:
    mappings = load_mappings([MAPPINGS], ["asterix", "sapient", "lattice", "picogrid_ecn"])
    return Gateway(mappings, [Destination(d, None, 100) for d in destinations])


def test_there_is_no_default_destination() -> None:
    with pytest.raises(ConfigError, match="no destinations"):
        Gateway([], [])


def test_a_config_without_destinations_stops_the_gateway(tmp_path: Path) -> None:
    config = tmp_path / "gateway.yml"
    config.write_text(f"mappings: [{MAPPINGS}]\n")
    with pytest.raises(ConfigError, match="There is no default destination"):
        gateway_from_config(config)
    assert main(["check", "--config", str(config)]) == 2


def test_refused_by_one_destination_means_sent_to_none(ws_position: dict[str, Any]) -> None:
    a, b = Recorder({"name": "a"}), Recorder({"name": "b", "needs": "humidity"})
    g = gateway(a, b)
    g.start()
    assert g.handle(ws_position) == REFUSED
    g.close()
    assert a.sent == [] and b.sent == []


def test_a_mapped_record_goes_to_every_destination(ws_records: list[dict[str, Any]]) -> None:
    a, b = Recorder({"name": "a"}), Recorder({"name": "b"})
    g = gateway(a, b)
    g.start()
    outcomes = [g.handle(r) for r in ws_records]
    g.close()
    assert outcomes == [SKIPPED, SKIPPED, SKIPPED, SENT, SKIPPED]
    assert a.sent == b.sent == ["KX7"]


def test_topics_limit_a_destination(ws_position: dict[str, Any]) -> None:
    a, b = Recorder({"name": "a"}), Recorder({"name": "b"})
    mappings = load_mappings([MAPPINGS], ["asterix", "sapient", "lattice", "picogrid_ecn"])
    g = Gateway(mappings, [Destination(a, {"WX/Position"}, 10), Destination(b, {"BUOY/Position"}, 10)])
    g.start()
    g.handle(ws_position)
    g.close()
    assert (a.sent, b.sent) == (["KX7"], [])


def test_a_full_queue_drops_and_counts(ws_position: dict[str, Any]) -> None:
    a = Recorder({"name": "a"})
    g = gateway(a)
    g.destinations[0] = Destination(a, None, 1)  # not started: nothing drains the queue
    g.handle(ws_position)
    g.handle(ws_position)
    assert g.destinations[0].counts.dropped == 1


def test_a_record_that_is_not_an_object_is_refused() -> None:
    g = gateway(Recorder({"name": "a"}))
    assert g.handle([1, 2]) == REFUSED


@pytest.fixture
def listener() -> Any:
    a = Recorder({"name": "a"})
    g = gateway(a)
    g.start()
    server = make_server(g, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], a
    server.shutdown()
    g.close()


def post(port: int, body: bytes, media_type: str = "application/json", path: str = "/records") -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", path, body=body, headers={"Content-Type": media_type})
    status = connection.getresponse().status
    connection.close()
    return status


def test_rest_listener_json_and_cbor(listener: Any, ws_position: dict[str, Any]) -> None:
    port, recorder = listener
    assert post(port, json.dumps(ws_position).encode()) == 204
    assert post(port, cbor2.dumps(ws_position), "application/cbor") == 204
    recorder.event.wait(5)
    assert recorder.sent[:1] == ["KX7"]


def test_rest_listener_answers(listener: Any, ws_position: dict[str, Any]) -> None:
    port, _ = listener
    broken = json.loads(json.dumps(ws_position))
    del broken["message"]["latitude"]
    assert post(port, json.dumps(broken).encode()) == 422
    assert post(port, b"{not json") == 400
    assert post(port, b"{}", "text/plain") == 415
    assert post(port, b"{}", path="/other") == 404
    assert post(port, json.dumps({"subsystem": "WX", "message_type": "Heartbeat"}).encode()) == 204


def test_shift_to_now_keeps_the_gaps(ws_records: list[dict[str, Any]]) -> None:
    moved = list(shift_to(iter(ws_records), 2_000_000_000 * 10**9))
    assert [r["timestamp"]["secs_since_epoch"] for r in moved] == [2_000_000_000 + i for i in range(5)]
    assert ws_records[0]["timestamp"]["secs_since_epoch"] == 1_750_000_000


def test_print_writes_payloads_and_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    status = main(
        ["print", "--mapping", str(MAPPINGS), "--adapter", "cot", str(RECORDS / "weather-station.records.jsonl")]
    )
    out = capsys.readouterr().out.splitlines()
    assert status == 0
    assert len(out) == 1 and out[0].startswith('<event version="2.0" uid="KX7"')


def test_replay_from_a_config(tmp_path: Path) -> None:
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    config = tmp_path / "gateway.yml"
    config.write_text(
        f"mappings: [{MAPPINGS}]\n"
        "destinations:\n"
        "  - name: local-display\n"
        "    adapter: cot\n"
        "    transport: udp\n"
        f"    address: 127.0.0.1:{listener.getsockname()[1]}\n"
    )
    assert main(["replay", "--config", str(config), str(RECORDS / "weather-station.records.jsonl")]) == 0
    assert listener.recv(65536).startswith(b'<event version="2.0" uid="KX7"')


def test_unknown_adapter_and_unknown_destination_key(tmp_path: Path) -> None:
    config = tmp_path / "gateway.yml"
    config.write_text(f"mappings: [{MAPPINGS}]\ndestinations:\n  - {{name: x, adapter: nothing}}\n")
    with pytest.raises(ConfigError, match="adapter 'nothing' is not installed"):
        gateway_from_config(config)
    config.write_text(
        f"mappings: [{MAPPINGS}]\ndestinations:\n"
        "  - {name: x, adapter: cot, transport: udp, address: '127.0.0.1:9', colour: red}\n"
    )
    with pytest.raises(ConfigError, match="unknown key 'colour'"):
        gateway_from_config(config)


def test_record_error_names_the_field() -> None:
    m = Mapping(
        {
            "match": {"subsystem": "A", "message_type": "B"},
            "uid": ["id"],
            "type": {"value": "a-u-G"},
            "position": {"geodetic": ["lat", "lon"]},
        },
        "t",
    )
    with pytest.raises(RecordError, match="message field 'id'"):
        m.track({"message": {"lat": 1, "lon": 2}, "timestamp": {"secs_since_epoch": 0, "nanos_since_epoch": 0}})
