"""Canonical Track v2 as input: turned into a record, then mapped by
mappings/canonical-track-v2.yml, and written by every open-standard adapter."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

import asterix4py
import pytest

from open_entity_gateway.adapters.asterix import LSB_DEGREES, AsterixCat062
from open_entity_gateway.adapters.cot import CursorOnTarget
from open_entity_gateway.canonical_track import is_canonical_track, track_record
from open_entity_gateway.gateway import REFUSED, SENT, Destination, Gateway, prepare_mappings
from open_entity_gateway.mapping import Mapping, MappingError
from open_entity_gateway.record import RecordError

from .conftest import MAPPINGS, ROOT

pytestmark = pytest.mark.usefixtures("each_encoder")

TRACKS = ROOT / "examples" / "tracks" / "example.tracks.jsonl"


def tracks() -> list[dict[str, Any]]:
    return [json.loads(line) for line in TRACKS.read_text().splitlines() if line.strip()]


def mapped(adapter: Any, track: dict[str, Any]) -> tuple[Any, Mapping, dict[str, Any]]:
    mappings = prepare_mappings([MAPPINGS], [adapter])
    record = track_record(track)
    (m,) = [m for m in mappings if m.matches(record)]
    return m.track(record), m, record


def test_the_examples_are_canonical_tracks() -> None:
    assert all(is_canonical_track(t) for t in tracks())
    assert not is_canonical_track({"apiVersion": "billie.dev/track-v1", "kind": "Track"})


def test_a_track_keeps_its_values() -> None:
    record = track_record(tracks()[0])
    body = record["message"]
    assert (record["subsystem"], record["message_type"], record["dictionary"]) == (
        "TRACK",
        "Track",
        "canonical-track-v2",
    )
    assert body["latitude"] == 64.8378 and body["longitude"] == -147.7164 and body["altitude"] == 412.0
    assert body["speed"] == pytest.approx(13.0)
    assert body["course"] == 90.0  # due east; a course every engine writes the same way
    # 2025-06-15T15:06:43.250Z
    assert record["timestamp"] == {"secs_since_epoch": 1750000003, "nanos_since_epoch": 250_000_000}


@pytest.mark.parametrize(
    ("change", "member"),
    [
        ({"trackId": ""}, "trackId"),
        ({"observedAt": "yesterday"}, "observedAt"),
        ({"position": {"latitudeDeg": "64", "longitudeDeg": -147.0}}, "position.latitudeDeg"),
        ({"trackGeneration": 3}, "trackGeneration"),
    ],
)
def test_a_track_missing_a_required_member_is_refused_by_name(change: dict[str, Any], member: str) -> None:
    with pytest.raises(RecordError, match=member.replace(".", r"\.")):
        track_record({**tracks()[0], **change})


def test_cot_from_a_track() -> None:
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    track, m, record = mapped(adapter, tracks()[0])
    assert track.uid == "example-radar-1_T-0007" and track.cot_type == "a-u-A-M-F-Q"
    event = ET.fromstring(adapter.encode(track, record, m.parsed.get(adapter.name)))
    point = event.find("point")
    assert point is not None and float(point.get("lat", "")) == 64.8378 and float(point.get("hae", "")) == 412.0


def test_a_track_without_altitude_or_velocity_still_maps() -> None:
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    track, _, _ = mapped(adapter, tracks()[1])
    assert track.hae is None and track.course is None and track.speed is None
    assert track.cot_type == "a-u-A"


def test_asterix_from_a_track() -> None:
    adapter = AsterixCat062({"name": "a", "adapter": "asterix-cat062", "print": True})
    track, m, record = mapped(adapter, tracks()[0])
    item = asterix4py.AsterixParser(adapter.encode(track, record, m.parsed[adapter.name])).get_result()[1]
    assert item["cat"] == 62
    assert item["105"]["Lat"] == pytest.approx(64.8378, abs=LSB_DEGREES)
    assert item["080"]["SIM"] == 1  # the example track is simulated test data


def test_a_live_track_is_not_marked_simulated() -> None:
    adapter = AsterixCat062({"name": "a", "adapter": "asterix-cat062", "print": True})
    live = {**tracks()[0], "simulated": False, "testData": False}
    track, m, record = mapped(adapter, live)
    item = asterix4py.AsterixParser(adapter.encode(track, record, m.parsed[adapter.name])).get_result()[1]
    assert item["080"].get("SIM", 0) == 0


def test_sapient_from_a_track() -> None:
    pytest.importorskip("google.protobuf")
    pytest.importorskip("open_entity_gateway.adapters.sapient._generated.sapient_msg.proto_options_pb2")
    from open_entity_gateway.adapters.sapient import SapientEdgeNode

    node = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
    adapter = SapientEdgeNode({"name": "s", "adapter": "sapient", "node_id": node, "address": "127.0.0.1:9"})
    track, m, record = mapped(adapter, tracks()[0])
    assert adapter.encode(track, record, m.parsed[adapter.name]) is not None


def test_sapient_writes_the_tracks_own_velocity_components() -> None:
    pytest.importorskip("google.protobuf")
    pytest.importorskip("open_entity_gateway.adapters.sapient._generated.sapient_msg.proto_options_pb2")
    from open_entity_gateway.adapters.sapient import SapientEdgeNode

    node = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
    adapter = SapientEdgeNode({"name": "s", "adapter": "sapient", "node_id": node, "address": "127.0.0.1:9"})
    track, m, record = mapped(adapter, {**tracks()[0], "velocity": {"eastMps": 13.0, "northMps": 0.0, "upMps": 0.0}})
    assert track.velocity == (13.0, 0.0)
    detection = adapter.encode(track, record, m.parsed[adapter.name])
    report = getattr(detection, "detection_report", detection)
    assert (report.enu_velocity.east_rate, report.enu_velocity.north_rate) == (13.0, 0.0)


def test_the_gateway_takes_a_track_where_it_takes_a_record() -> None:
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    gateway = Gateway(prepare_mappings([MAPPINGS], [adapter]), [Destination(adapter, None, 10)])
    assert gateway.handle(tracks()[0]) == SENT
    assert gateway.handle({**tracks()[0], "position": {}}) == REFUSED


def test_optional_names_only_the_altitude_or_motion_fields() -> None:
    document = {
        "match": {"subsystem": "X", "message_type": "Y"},
        "uid": ["id"],
        "type": {"value": "a-f-G"},
        "position": {"geodetic": ["lat", "lon"]},
        "optional": ["lat"],
    }
    with pytest.raises(MappingError, match="optional"):
        Mapping(document, "m")


def test_print_writes_a_track(capsys: Any) -> None:
    from open_entity_gateway.cli import main

    assert main(["print", "--mapping", str(MAPPINGS), "--adapter", "cot", str(TRACKS)]) == 0
    out = capsys.readouterr().out
    assert 'uid="example-radar-1_T-0007"' in out and 'uid="example-radar-1_T-0008"' in out


def test_shift_to_now_moves_observed_at_and_keeps_the_gaps() -> None:
    from open_entity_gateway.canonical_track import observed_ns
    from open_entity_gateway.inputs.replay import read_records, rfc3339, shift_to

    with TRACKS.open() as stream:
        tracks = list(read_records(stream))
    start = 2_000_000_000 * 10**9 + 125_000_000
    moved = list(shift_to(iter(tracks), start))
    assert moved[0]["observedAt"] == "2033-05-18T03:33:20.125Z"
    gaps = [observed_ns(t["observedAt"]) - observed_ns(tracks[0]["observedAt"]) for t in tracks]
    assert [observed_ns(t["observedAt"]) - start for t in moved] == gaps
    assert {k: v for k, v in moved[0].items() if k != "observedAt"} == {
        k: v for k, v in tracks[0].items() if k != "observedAt"
    }
    assert rfc3339(start + 7) == "2033-05-18T03:33:20.125000007Z"


def test_shift_to_now_passes_an_unreadable_observed_at_on_unchanged() -> None:
    from open_entity_gateway.inputs.replay import shift_to

    track = {"apiVersion": "billie.dev/track-v2", "kind": "Track", "observedAt": "yesterday"}
    assert list(shift_to(iter([track]), 10**18)) == [track]
