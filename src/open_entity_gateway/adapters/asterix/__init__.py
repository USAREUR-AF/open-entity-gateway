"""ASTERIX category 062 (system track data): one data block with one record
per track, over UDP. See docs/adapters/asterix.md.

Items written, in the order of the category's user application profile:

    I062/010  data source identifier: SAC and SIC of the destination   (mandatory)
    I062/015  service identification, when the destination gives one
    I062/070  time of track information, 1/128 s since UTC midnight     (mandatory)
    I062/105  calculated position in WGS-84, 180/2^25 degree
    I062/185  calculated velocity, Vx east and Vy north, 0.25 m/s, when the mapping has motion
    I062/040  track number                                              (mandatory)
    I062/080  track status: monosensor, the altitude source, SIM when simulated   (mandatory)
    I062/130  calculated geometric altitude, 6.25 ft, when the mapping gives an altitude
"""

from __future__ import annotations

import math
import struct
import threading
from typing import Any

from ...adapter import Adapter, ConfigError
from ...mapping import Mapping, MappingError, Track, Value
from ...record import NANOS, RecordError, field_text
from ...transport import Datagrams

CATEGORY = 62
LSB_DEGREES = 180 / 2**25
LSB_VELOCITY = 0.25  # m/s
LSB_ALTITUDE_FT = 6.25
FEET_PER_METRE = 1 / 0.3048
ALTITUDE_RANGE_FT = (-1500.0, 150000.0)
VELOCITY_RANGE = (-8192.0, 8191.75)
SECONDS_PER_DAY = 86400

# I062/080 octet 1 bits 5/3 (SRC): source of the calculated altitude.
ALTITUDE_SOURCES = {
    "none": 0,
    "gnss": 1,
    "3d-radar": 2,
    "triangulation": 3,
    "height-from-coverage": 4,
    "speed-look-up-table": 5,
    "default-height": 6,
    "multilateration": 7,
}

# Field reference numbers of the items written (category 062 UAP).
FRN = {"010": 1, "015": 3, "070": 4, "105": 5, "185": 7, "040": 12, "080": 13, "130": 18}

_KEYS = {
    "name",
    "adapter",
    "topics",
    "queue",
    "address",
    "ttl",
    "interface",
    "source_port",
    "sac",
    "sic",
    "service_id",
    "print",
}
_BLOCK_KEYS = {"track_number", "simulated", "altitude_source"}


def fspec(frns: list[int]) -> bytes:
    """The field specification for the given field reference numbers."""
    octets = [0] * ((max(frns) + 6) // 7)
    for frn in frns:
        index, position = divmod(frn - 1, 7)
        octets[index] |= 0x80 >> position
    for index in range(len(octets) - 1):
        octets[index] |= 0x01
    return bytes(octets)


def _byte(destination: dict[str, Any], key: str, label: str, required: bool) -> int | None:
    if key not in destination:
        if required:
            raise ConfigError(f"destination {label!r}: {key} is required (0 to 255)")
        return None
    value = destination[key]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise ConfigError(f"destination {label!r}: {key} is a whole number from 0 to 255")
    return value


class Block:
    def __init__(self, track_number: Value | None, simulated: bool | str, altitude_source: int):
        self.track_number = track_number
        self.simulated = simulated
        self.altitude_source = altitude_source


class AsterixCat062(Adapter):
    name = "asterix-cat062"
    block = "asterix"

    def __init__(self, destination: dict[str, Any]):
        super().__init__(destination)
        unknown = set(destination) - _KEYS
        if unknown:
            raise ConfigError(f"destination {self.label!r}: unknown key {sorted(unknown)[0]!r}")
        self.printing = bool(destination.get("print"))
        self.sac = _byte(destination, "sac", self.label, not self.printing) or 0
        self.sic = _byte(destination, "sic", self.label, not self.printing) or 0
        self.service_id = _byte(destination, "service_id", self.label, False)
        self.transport = None if self.printing else Datagrams(destination, self.label)
        self._numbers: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def parse_block(cls, block: Any, mapping: Mapping) -> Block:
        where = f"{mapping.name}: asterix"
        if block is None:
            return Block(None, False, 0)
        if not isinstance(block, dict) or set(block) - _BLOCK_KEYS:
            raise MappingError(f"{where}: takes {sorted(_BLOCK_KEYS)}")
        simulated = block.get("simulated", False)
        if isinstance(simulated, dict) and set(simulated) == {"field"} and isinstance(simulated["field"], str):
            simulated = simulated["field"]  # a message field holding true or false
        elif not isinstance(simulated, bool):
            raise MappingError(f"{where}.simulated: true, false, or {{ field: <a field holding true or false> }}")
        source = block.get("altitude_source", "none")
        if source not in ALTITUDE_SOURCES:
            raise MappingError(f"{where}.altitude_source: one of {sorted(ALTITUDE_SOURCES)}")
        number = Value(block["track_number"], f"{where}.track_number") if "track_number" in block else None
        return Block(number, simulated, ALTITUDE_SOURCES[source])

    @staticmethod
    def simulated(record: dict[str, Any], block: Block) -> bool:
        if isinstance(block.simulated, bool):
            return block.simulated
        text = field_text(record, block.simulated)
        if text not in ("true", "false"):
            raise RecordError(f"message field {block.simulated!r} is not true or false: {text!r}")
        return text == "true"

    def track_number(self, track: Track, record: dict[str, Any], block: Block) -> int:
        if block.track_number is not None:
            text = block.track_number.resolve(record)
            try:
                number = int(float(text))
            except ValueError:
                raise RecordError(f"asterix.track_number {text!r} is not a number") from None
            if not 0 <= number <= 0xFFFF or number != float(text):
                raise RecordError(f"asterix.track_number {text!r} is not a whole number from 0 to 65535")
            return number
        # No number in the record: each uid gets the next free number, in the
        # order uids are first seen, for the life of the process.
        with self._lock:
            if track.uid not in self._numbers:
                if len(self._numbers) >= 0xFFFF:
                    raise RecordError("asterix: all 65535 track numbers are in use")
                self._numbers[track.uid] = len(self._numbers) + 1
            return self._numbers[track.uid]

    def encode(self, track: Track, record: dict[str, Any], block: Block | None) -> bytes:
        block = block or Block(None, False, 0)
        items: dict[str, bytes] = {"010": bytes((self.sac, self.sic))}
        if self.service_id is not None:
            items["015"] = bytes((self.service_id,))

        seconds_of_day = (track.time_ns / NANOS) % SECONDS_PER_DAY
        ticks = round(seconds_of_day * 128) % (SECONDS_PER_DAY * 128)
        items["070"] = ticks.to_bytes(3, "big")

        latitude = round(track.latitude / LSB_DEGREES)
        longitude = track.longitude if track.longitude < 180 else track.longitude - 360
        longitude_lsb = round(longitude / LSB_DEGREES)
        latitude = max(min(latitude, 2**24), -(2**24))
        if longitude_lsb >= 2**25:
            longitude_lsb -= 2**26
        items["105"] = struct.pack(">ii", latitude, longitude_lsb)

        if track.course is not None and track.speed is not None:
            course = math.radians(track.course)
            vx, vy = track.speed * math.sin(course), track.speed * math.cos(course)
            for axis, value in (("Vx", vx), ("Vy", vy)):
                if not VELOCITY_RANGE[0] <= value <= VELOCITY_RANGE[1]:
                    raise RecordError(f"asterix I062/185 {axis} {value:.2f} m/s is outside its range")
            items["185"] = struct.pack(">hh", round(vx / LSB_VELOCITY), round(vy / LSB_VELOCITY))

        items["040"] = self.track_number(track, record, block).to_bytes(2, "big")

        # MON=1 (monosensor), MRH=1 (geometric altitude) when an altitude is sent.
        first = 0x80 | (0x20 if track.hae is not None else 0) | (block.altitude_source << 2)
        if self.simulated(record, block):
            items["080"] = bytes((first | 0x01, 0x80))  # first extent: SIM
        else:
            items["080"] = bytes((first,))

        if track.hae is not None:
            feet = track.hae * FEET_PER_METRE
            if not ALTITUDE_RANGE_FT[0] <= feet <= ALTITUDE_RANGE_FT[1]:
                raise RecordError(f"asterix I062/130: altitude {track.hae} m is outside -1500 ft to 150000 ft")
            items["130"] = struct.pack(">h", round(feet / LSB_ALTITUDE_FT))

        order = sorted(items, key=lambda item: FRN[item])
        body = fspec([FRN[item] for item in order]) + b"".join(items[item] for item in order)
        return bytes((CATEGORY,)) + (3 + len(body)).to_bytes(2, "big") + body

    def describe(self, payload: bytes) -> str:
        return payload.hex()

    def start(self) -> None:
        if self.transport is not None:
            self.transport.open()

    def send(self, payload: bytes) -> None:
        assert self.transport is not None
        self.transport.send(payload)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
