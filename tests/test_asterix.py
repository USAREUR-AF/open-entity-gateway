"""ASTERIX category 062: every record the encoder writes is decoded by
asterix4py (MIT; its category 062 edition 1.18 description), field by field."""

from __future__ import annotations

import math
import socket
from typing import Any

import asterix4py
import pytest

from open_entity_gateway.adapters.asterix import LSB_DEGREES, AsterixCat062, fspec
from open_entity_gateway.gateway import prepare_mappings
from open_entity_gateway.mapping import Mapping
from open_entity_gateway.record import RecordError

from .conftest import MAPPINGS


def decode(payload: bytes) -> dict[str, Any]:
    result = asterix4py.AsterixParser(payload).get_result()
    assert list(result) == [1], "one record per data block"
    record: dict[str, Any] = result[1]
    assert record["cat"] == 62
    return record


def adapter(**destination: Any) -> AsterixCat062:
    return AsterixCat062({"name": "a", "adapter": "asterix-cat062", "print": True, **destination})


def encode_example(record: dict[str, Any], **destination: Any) -> tuple[bytes, Mapping]:
    a = adapter(**destination)
    mappings = prepare_mappings([MAPPINGS], [a])
    (m,) = [m for m in mappings if m.matches(record)]
    return a.encode(m.track(record), record, m.parsed[a.name]), m


def test_fspec() -> None:
    assert fspec([1, 4, 5]) == bytes([0b10011000])
    assert fspec([1, 4, 5, 12, 13]) == bytes([0b10011001, 0b00001100])
    assert fspec([1, 18]) == bytes([0b10000001, 0b00000001, 0b00010000])


def test_weather_station_round_trip(ws_position: dict[str, Any]) -> None:
    payload, _ = encode_example(ws_position, sac=25, sic=4, service_id=3)
    assert payload[0] == 62 and int.from_bytes(payload[1:3], "big") == len(payload)
    item = decode(payload)
    assert item["010"] == {"SAC": 25, "SIC": 4}
    assert item["015"]["SID"] == 3 if "SID" in item["015"] else list(item["015"].values()) == [3]
    # 1750000003 s is 15:06:43 UTC: 54403 s after midnight.
    assert item["070"]["ToT"] == 54403.0
    assert item["105"]["Lat"] == pytest.approx(64.8378, abs=LSB_DEGREES)
    assert item["105"]["Lon"] == pytest.approx(-147.7164, abs=LSB_DEGREES)
    assert item["040"]["TrkN"] == 1  # first uid seen
    status = item["080"]
    assert (status["MON"], status["MRH"], status["SRC"], status["CNF"], status["SIM"]) == (1, 1, 1, 0, 1)
    # 136.5 m is 447.8 ft; the LSB is 6.25 ft.
    assert item["130"]["Alt"] == pytest.approx(136.5 / 0.3048, abs=6.25 / 2)
    assert "185" not in item and "245" not in item


def test_harbour_buoy_round_trip_with_velocity_and_track_number(buoy_position: dict[str, Any]) -> None:
    payload, _ = encode_example(buoy_position, sac=1, sic=2)
    item = decode(payload)
    assert item["040"]["TrkN"] == 7  # asterix.track_number: { field: buoy_number }
    assert item["105"]["Lat"] == pytest.approx(44.5012345, abs=LSB_DEGREES)
    assert item["105"]["Lon"] == pytest.approx(-30.254321, abs=LSB_DEGREES)
    # 0.35 m/s towards 135 degrees: east +0.247, north -0.247; LSB 0.25 m/s.
    assert item["185"]["Vx"] == pytest.approx(0.35 * math.sin(math.radians(135)), abs=0.125)
    assert item["185"]["Vy"] == pytest.approx(0.35 * math.cos(math.radians(135)), abs=0.125)
    assert item["130"]["Alt"] == pytest.approx(-1.5 / 0.3048, abs=6.25 / 2)
    assert item["080"]["SRC"] == 0


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(0.0, 0.0), (89.99999, 179.99999), (-90.0, -180.0), (51.4700, -0.4543), (-33.9461, 151.1772), (12.3, 180.0)],
)
def test_positions_round_trip(lat: float, lon: float) -> None:
    m = Mapping(
        {
            "match": {"subsystem": "A", "message_type": "B"},
            "uid": ["id"],
            "type": {"value": "a-u-A"},
            "position": {"geodetic": ["lat", "lon", "alt"]},
        },
        "t",
    )
    record = {
        "message": {"id": "x", "lat": lat, "lon": lon, "alt": 10000.0},
        "timestamp": {"secs_since_epoch": 86399, "nanos_since_epoch": 992_187_500},
    }
    item = decode(adapter().encode(m.track(record), record, None))
    expected_lon = lon - 360 if lon >= 180 else lon
    assert item["105"]["Lat"] == pytest.approx(lat, abs=LSB_DEGREES)
    assert item["105"]["Lon"] == pytest.approx(expected_lon, abs=LSB_DEGREES)
    assert item["070"]["ToT"] == pytest.approx(86399.9921875)


def test_out_of_range_values_are_refused() -> None:
    m = Mapping(
        {
            "match": {"subsystem": "A", "message_type": "B"},
            "uid": ["id"],
            "type": {"value": "a-u-A"},
            "position": {"geodetic": ["lat", "lon", "alt"]},
        },
        "t",
    )
    record = {
        "message": {"id": "x", "lat": 1, "lon": 1, "alt": 50000.0},
        "timestamp": {"secs_since_epoch": 0, "nanos_since_epoch": 0},
    }
    with pytest.raises(RecordError, match="I062/130"):
        adapter().encode(m.track(record), record, None)


def test_a_track_number_that_is_not_a_number_is_refused(buoy_position: dict[str, Any]) -> None:
    record = {**buoy_position, "message": {**buoy_position["message"], "buoy_number": "seven"}}
    with pytest.raises(RecordError, match="track_number"):
        encode_example(record, sac=1, sic=2)


def test_sent_over_udp(ws_position: dict[str, Any]) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    a = AsterixCat062(
        {
            "name": "a",
            "adapter": "asterix-cat062",
            "sac": 9,
            "sic": 9,
            "address": f"127.0.0.1:{listener.getsockname()[1]}",
        }
    )
    mappings = prepare_mappings([MAPPINGS], [a])
    (m,) = [m for m in mappings if m.matches(ws_position)]
    a.start()
    a.send(a.encode(m.track(ws_position), ws_position, m.parsed[a.name]))
    a.close()
    assert decode(listener.recv(65536))["010"] == {"SAC": 9, "SIC": 9}


def test_sac_and_sic_are_required() -> None:
    from open_entity_gateway.adapter import ConfigError

    with pytest.raises(ConfigError, match="sac is required"):
        AsterixCat062({"name": "a", "adapter": "asterix-cat062", "address": "127.0.0.1:9"})
