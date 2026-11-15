from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from open_entity_gateway.mapping import Mapping, MappingError, load_mappings, select
from open_entity_gateway.record import RecordError, decimal_text, json_text, rfc3339_offset, rfc3339_z

from .conftest import MAPPINGS

BASE = """
match: {subsystem: WX, message_type: Position}
uid: [station_id]
type: {value: "a-f-G"}
position: {geodetic: [latitude, longitude, altitude]}
stale_minutes: 10
"""


def mapping(extra: str = "", base: str = BASE, blocks: tuple[str, ...] = ()) -> Mapping:
    return Mapping(yaml.safe_load(base + extra), "test.yml", blocks)


def test_the_cot_block_of_first_bite_is_a_mapping(ws_position: dict[str, Any]) -> None:
    track = mapping().track(ws_position)
    assert (track.uid, track.cot_type) == ("KX7", "a-f-G")
    assert (track.latitude, track.longitude, track.hae) == (64.8378, -147.7164, 136.5)
    assert (track.affiliation, track.dimension) == ("friend", "ground")


def test_time_is_the_capture_time_by_default(ws_position: dict[str, Any]) -> None:
    track = mapping().track(ws_position, received_ns=5)
    assert track.time_ns == 1_750_000_003 * 10**9
    assert track.stale_ns - track.time_ns == 10 * 60 * 10**9
    assert mapping("time: received\n").track(ws_position, received_ns=5).time_ns == 5


def test_stale_minutes_defaults_to_sixty_as_first_bite(ws_position: dict[str, Any]) -> None:
    m = mapping(base=BASE.replace("stale_minutes: 10\n", ""))
    track = m.track(ws_position)
    assert track.stale_ns - track.time_ns == 60 * 60 * 10**9


@pytest.mark.parametrize(
    ("field", "change", "named"),
    [
        ("latitude", None, "'latitude' is missing"),
        ("longitude", "east", "'longitude' is not a number"),
        ("latitude", 91.0, "'latitude' is not a latitude"),
        ("station_id", None, "'station_id' is missing"),
        ("altitude", float("nan"), "'altitude' is not a finite number"),
    ],
)
def test_a_record_that_cannot_fill_the_mapping_is_refused_by_field(
    ws_position: dict[str, Any], field: str, change: Any, named: str
) -> None:
    record = copy.deepcopy(ws_position)
    if change is None:
        del record["message"][field]
    else:
        record["message"][field] = change
    with pytest.raises(RecordError, match=named):
        mapping().track(record)


def test_a_record_without_a_capture_time_is_refused_by_member(ws_position: dict[str, Any]) -> None:
    record = {**ws_position, "timestamp": None}
    with pytest.raises(RecordError, match="'timestamp'"):
        mapping().track(record)


def test_version_1_text_values_give_the_same_track(ws_position: dict[str, Any]) -> None:
    v1 = copy.deepcopy(ws_position)
    v1["message"] = {k: str(v) for k, v in v1["message"].items()}
    assert mapping().track(v1) == mapping().track(ws_position)


def test_type_lookup_as_first_bite_with_a_default(ws_position: dict[str, Any]) -> None:
    m = mapping(
        base=BASE.replace('type: {value: "a-f-G"}', "type: {field: station_id, values: {KX7: a-h-A}, default: a-u-G}")
    )
    assert m.track(ws_position).cot_type == "a-h-A"
    other = copy.deepcopy(ws_position)
    other["message"]["station_id"] = "KX8"
    assert m.track(other).cot_type == "a-u-G"
    del other["message"]["station_id"]
    with pytest.raises(RecordError, match="station_id"):
        m.track(other)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (("uid: [station_id]", "uid: []"), "uid: name at least one field"),
        (('type: {value: "a-f-G"}', "type: {}"), "type: give `value` or `field`"),
        (('type: {value: "a-f-G"}', "type: {value: x, field: f, default: d}"), "not both"),
        (('type: {value: "a-f-G"}', "type: {value: x, default: d}"), "only together with `field`"),
        (('type: {value: "a-f-G"}', "type: {field: f}"), "`default` is required"),
        (("position: {geodetic: [latitude, longitude, altitude]}", "position: {}"), "give `geodetic` or `ecef`"),
        (
            (
                "position: {geodetic: [latitude, longitude, altitude]}",
                "position: {geodetic: [a, b, c], ecef: [x, y, z]}",
            ),
            "not both",
        ),
        (("position: {geodetic: [latitude, longitude, altitude]}", "position: {ecef: [x, y]}"), "position.ecef"),
        (("stale_minutes: 10", "stale_minuets: 10"), "unknown key 'stale_minuets'"),
        (("stale_minutes: 10", "stale_minutes: 10\nform: flat"), "form: flat is not accepted"),
    ],
)
def test_unusable_mappings_say_why(change: tuple[str, str], error: str) -> None:
    with pytest.raises(MappingError, match=error):
        mapping(base=BASE.replace(*change))


def test_ecef_is_converted_as_first_bite_converts_it() -> None:
    m = mapping(base=BASE.replace("geodetic: [latitude, longitude, altitude]", "ecef: [x, y, z]"))
    record = {
        "timestamp": {"secs_since_epoch": 0, "nanos_since_epoch": 0},
        "message": {"station_id": "t", "x": 4201133.037, "y": 168331.004, "z": 4780438.968},
    }
    track = m.track(record)
    # first-bite cotify's own test: 300 m above the ellipsoid at 48.8584 N, 2.2945 E.
    assert track.latitude == pytest.approx(48.8584, abs=1e-6)
    assert track.longitude == pytest.approx(2.2945, abs=1e-6)
    assert track.hae == pytest.approx(300.0, abs=1e-3)


def test_name_and_motion_are_gateway_keys(buoy_position: dict[str, Any]) -> None:
    (m,) = load_mappings([MAPPINGS / "harbour-buoy.position.yml"], ["asterix", "sapient"])
    track = m.track(buoy_position)
    assert track.name == "Buoy/7"
    assert (track.course, track.speed) == (135.0, 0.35)
    assert (track.cot_type, track.dimension) == ("a-f-S", "sea-surface")


def test_mapping_files_load_and_select(ws_records: list[dict[str, Any]]) -> None:
    mappings = load_mappings([MAPPINGS], ["asterix", "sapient", "lattice", "picogrid_ecn"])
    assert sorted(m.topic for m in mappings) == ["BUOY/Position", "WX/Position"]
    chosen = [select(mappings, r) for r in ws_records]
    assert [m.topic if m else None for m in chosen] == [None, None, None, "WX/Position", None]


def test_an_adapter_block_is_unknown_unless_an_adapter_takes_it() -> None:
    with pytest.raises(MappingError, match="unknown key 'sapient'"):
        mapping("sapient: {}\n")
    assert mapping("sapient: {}\n", blocks=("sapient",)).blocks == {"sapient": {}}


def test_two_mappings_for_the_same_records_are_refused(tmp_path: Any) -> None:
    (tmp_path / "a.yml").write_text(BASE)
    (tmp_path / "b.yml").write_text(BASE)
    with pytest.raises(MappingError, match="match the same records"):
        load_mappings([tmp_path])


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (136.5, "136.5"),
        (100.0, "100"),
        (1e-7, "0.0000001"),
        (-180.0, "-180"),
        (1e21, "1000000000000000000000"),
        (-0.0, "-0"),
        (48.85839999837629, "48.85839999837629"),
    ],
)
def test_numbers_are_written_as_first_bite_writes_them(number: float, text: str) -> None:
    assert decimal_text(number) == text


def test_text_of_json_values() -> None:
    assert [json_text(v) for v in (True, 7, 7.0, 1e-7, 1e21)] == ["true", "7", "7.0", "1e-7", "1e21"]


def test_times() -> None:
    assert rfc3339_offset(1_750_000_003 * 10**9) == "2025-06-15T15:06:43+00:00"
    assert rfc3339_offset(1_750_000_003 * 10**9 + 709_165_450) == "2025-06-15T15:06:43.709165450+00:00"
    assert rfc3339_offset(1_750_000_003 * 10**9 + 709_000_000) == "2025-06-15T15:06:43.709+00:00"
    assert rfc3339_z(1_750_000_003 * 10**9 + 500_000_000) == "2025-06-15T15:06:43.5Z"
