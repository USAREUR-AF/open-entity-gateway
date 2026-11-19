"""Mapping files: which records become tracks, and where each value of a
track comes from.

A mapping is data, one YAML file per message. It holds no message format of
its own: it names fields of a record's `message`.

The keys `uid`, `type`, `position`, `stale_minutes` and `form` are the keys of
first-bite's `cot:` mapping (first-bite cotify/README.md) and mean the same
here, so the block a first-bite subscriber carries under `cot:` can be pasted
into a mapping file unchanged. docs/mapping.md lists where the two differ.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .record import (
    NANOS,
    RECORD_MEMBERS,
    RecordError,
    capture_ns,
    field_number,
    field_text,
    member_text,
    message,
)

# first-bite cotify: DEFAULT_STALE_MINUTES.
DEFAULT_STALE_MINUTES = 60
TIME_SOURCES = ("capture", "received")
CORE_KEYS = frozenset(
    {"match", "uid", "type", "position", "stale_minutes", "form", "time", "name", "motion", "optional"}
)
MATCH_KEYS = frozenset({"subsystem", "message_type", "dictionary"})

# The affiliation and battle-dimension atoms of a CoT atom type `a-<affiliation>-<dimension>-…`
# (MITRE CoT event schema, public release, documentation of the `type` attribute).
AFFILIATIONS = {
    "p": "pending",
    "u": "unknown",
    "a": "assumed-friend",
    "f": "friend",
    "n": "neutral",
    "s": "suspect",
    "h": "hostile",
    "j": "joker",
    "k": "faker",
    "o": "none",
    "x": "other",
}
DIMENSIONS = {
    "P": "space",
    "A": "air",
    "G": "ground",
    "S": "sea-surface",
    "U": "subsurface",
    "F": "sof",
    "X": "other",
    "Z": "unknown",
}


class MappingError(ValueError):
    """A mapping file is wrong. Raised when it is loaded, naming file and key."""


class Value:
    """Where one text value comes from. In a mapping file:

    "text"                                   a constant
    { value: "text" }                        a constant
    { field: station_id }                    a field of the record's message
    { record: subsystem }                    a member of the record itself
    { field: kind, values: {A: X}, default: Y }   a lookup (field or record)
    [ <value>, <value>, ... ]                the parts joined with "/"
    """

    def __init__(self, spec: Any, where: str):
        self.where = where
        self.parts: list[Value] = []
        self.constant: str | None = None
        self.field: str | None = None
        self.member: str | None = None
        self.values: dict[str, str] | None = None
        self.default: str | None = None
        if isinstance(spec, list):
            if not spec:
                raise MappingError(f"{where}: an empty list")
            self.parts = [Value(part, f"{where}[{i}]") for i, part in enumerate(spec)]
        elif isinstance(spec, (str, int, float)) and not isinstance(spec, bool):
            self.constant = str(spec)
        elif isinstance(spec, dict):
            unknown = set(spec) - {"value", "field", "record", "values", "default"}
            if unknown:
                raise MappingError(f"{where}: unknown key {sorted(unknown)[0]!r}")
            given = [key for key in ("value", "field", "record") if key in spec]
            if len(given) != 1:
                raise MappingError(f"{where}: give exactly one of value, field, record")
            if "value" in spec:
                if "values" in spec or "default" in spec:
                    raise MappingError(f"{where}: a constant takes no values or default")
                self.constant = str(spec["value"])
            elif "field" in spec:
                self.field = str(spec["field"])
            else:
                self.member = str(spec["record"])
                if self.member not in RECORD_MEMBERS:
                    raise MappingError(f"{where}: record member {self.member!r} is not one of {sorted(RECORD_MEMBERS)}")
            if "values" in spec:
                if not isinstance(spec["values"], dict) or not spec["values"]:
                    raise MappingError(f"{where}: values must be a table of value to result")
                self.values = {_key(k): str(v) for k, v in spec["values"].items()}
            if "default" in spec:
                if self.values is None:
                    raise MappingError(f"{where}: default needs values")
                self.default = str(spec["default"])
        else:
            raise MappingError(f"{where}: expected text, a table or a list")

    def results(self) -> set[str] | None:
        """Every value this can give, when that is known when the file is loaded."""
        if self.constant is not None:
            return {self.constant}
        if self.values is not None:
            return set(self.values.values()) | ({self.default} if self.default is not None else set())
        if self.parts:
            return None
        return None

    def resolve(self, record: dict[str, Any]) -> str:
        if self.parts:
            return "/".join(part.resolve(record) for part in self.parts)
        if self.constant is not None:
            return self.constant
        try:
            if self.field is not None:
                text: str | None = field_text(record, self.field)
            else:
                assert self.member is not None
                text = member_text(record, self.member)
        except RecordError:
            if self.values is not None and self.default is not None:
                return self.default
            raise
        if self.values is not None:
            if text in self.values:
                return self.values[text]
            if self.default is not None:
                return self.default
            named = f"message field {self.field!r}" if self.field else f"record member {self.member!r}"
            raise RecordError(f"{named} value {text!r} has no entry in {self.where}")
        if not text:
            named = f"message field {self.field!r}" if self.field else f"record member {self.member!r}"
            raise RecordError(f"{named} is empty")
        return text


def _key(key: Any) -> str:
    # YAML reads `true:` and `7:` as a boolean and a number; the record's text
    # form of those is `true` and `7`.
    if isinstance(key, bool):
        return "true" if key else "false"
    return str(key)


def optional_value(block: dict[str, Any], key: str, where: str) -> Value | None:
    return Value(block[key], f"{where}.{key}") if key in block else None


def constrained_value(block: dict[str, Any], key: str, where: str, allowed: Iterable[str]) -> Value:
    """A constant or a lookup whose every result is one of `allowed`."""
    made = Value(block[key], f"{where}.{key}")
    results = made.results()
    allowed = set(allowed)
    if results is None:
        raise MappingError(f"{where}.{key}: must be a constant or a lookup with values")
    bad = sorted(results - allowed)
    if bad:
        raise MappingError(f"{where}.{key}: {bad[0]!r} is not one of {sorted(allowed)}")
    return made


@dataclass(frozen=True)
class Source:
    """Which record a track came from."""

    subsystem: str
    message_type: str
    dictionary: str | None
    dictionary_version: str | None


@dataclass(frozen=True)
class Track:
    """One track, in the gateway's own terms. Each adapter writes it in its
    interface's form."""

    uid: str
    cot_type: str
    latitude: float
    longitude: float
    hae: float | None  # metres above the WGS 84 ellipsoid; None when not mapped
    time_ns: int  # the moment the track describes
    stale_ns: int  # the moment after which it should not be shown
    name: str | None
    course: float | None  # degrees clockwise from true north
    speed: float | None  # metres per second
    source: Source
    # metres per second east and north, when the source gave them as components;
    # an adapter that writes components uses these rather than course and speed
    velocity: tuple[float, float] | None = None

    @property
    def affiliation(self) -> str:
        """From the CoT type: `friend`, `hostile`, `neutral`, … or `unknown`."""
        atoms = self.cot_type.split("-")
        if len(atoms) >= 2 and atoms[0] == "a":
            return AFFILIATIONS.get(atoms[1], "unknown")
        return "unknown"

    @property
    def dimension(self) -> str:
        """From the CoT type: `air`, `ground`, `sea-surface`, … or `unknown`."""
        atoms = self.cot_type.split("-")
        if len(atoms) >= 3 and atoms[0] == "a":
            return DIMENSIONS.get(atoms[2], "unknown")
        return "unknown"


class Mapping:
    """One mapping file."""

    def __init__(self, document: Any, name: str, blocks: Iterable[str] = ()):
        self.name = name
        if not isinstance(document, dict):
            raise MappingError(f"{name}: a mapping is a table with match, uid, type and position")
        block_names = set(blocks)
        unknown = set(document) - CORE_KEYS - block_names
        if unknown:
            raise MappingError(
                f"{name}: unknown key {sorted(unknown)[0]!r}"
                + (f" (adapter blocks known: {sorted(block_names)})" if block_names else "")
            )

        match = document.get("match")
        if not isinstance(match, dict) or not {"subsystem", "message_type"} <= set(match):
            raise MappingError(f"{name}: match needs subsystem and message_type")
        if set(match) - MATCH_KEYS:
            raise MappingError(f"{name}: match: unknown key {sorted(set(match) - MATCH_KEYS)[0]!r}")
        self.match = {key: str(value) for key, value in match.items()}

        # -- the keys of first-bite's cot mapping -----------------------------
        uid = document.get("uid")
        if not isinstance(uid, list) or not uid or not all(isinstance(f, str) and f for f in uid):
            raise MappingError(f"{name}: uid: name at least one field")
        self.uid: list[str] = uid

        kind = document.get("type")
        if not isinstance(kind, dict):
            raise MappingError(f"{name}: type: give `value` or `field`")
        if set(kind) - {"value", "field", "values", "default"}:
            raise MappingError(
                f"{name}: type: unknown key {sorted(set(kind) - {'value', 'field', 'values', 'default'})[0]!r}"
            )
        if "value" in kind and "field" in kind:
            raise MappingError(f"{name}: type: give either `value` or `field`, not both")
        self.type_constant: str | None = None
        self.type_field: str | None = None
        self.type_values: dict[str, str] = {}
        self.type_default = ""
        if "value" in kind:
            if "values" in kind or "default" in kind:
                raise MappingError(f"{name}: type: `values` and `default` apply only together with `field`")
            self.type_constant = str(kind["value"])
        elif "field" in kind:
            if "default" not in kind:
                raise MappingError(f"{name}: type: `default` is required together with `field`")
            values = kind.get("values") or {}
            if not isinstance(values, dict):
                raise MappingError(f"{name}: type.values: a table of field value to CoT type")
            self.type_field = str(kind["field"])
            self.type_values = {_key(k): str(v) for k, v in values.items()}
            self.type_default = str(kind["default"])
        else:
            raise MappingError(f"{name}: type: give `value` or `field`")

        position = document.get("position")
        if not isinstance(position, dict) or not position:
            raise MappingError(f"{name}: position: give `geodetic` or `ecef`")
        if set(position) - {"geodetic", "ecef"}:
            raise MappingError(f"{name}: position: unknown key {sorted(set(position) - {'geodetic', 'ecef'})[0]!r}")
        if len(position) != 1:
            raise MappingError(f"{name}: position: give either `geodetic` or `ecef`, not both")
        ((self.frame, fields),) = position.items()
        sizes = (2, 3) if self.frame == "geodetic" else (3,)
        if (
            not isinstance(fields, list)
            or len(fields) not in sizes
            or not all(isinstance(f, str) and f for f in fields)
        ):
            raise MappingError(
                f"{name}: position.{self.frame}: "
                + (
                    "[latitude, longitude, altitude] or [latitude, longitude]"
                    if self.frame == "geodetic"
                    else "[x, y, z]"
                )
                + ", each a field name"
            )
        self.position_fields: list[str] = fields

        stale = document.get("stale_minutes", DEFAULT_STALE_MINUTES)
        if isinstance(stale, bool) or not isinstance(stale, int) or stale < 0:
            raise MappingError(f"{name}: stale_minutes: a whole number of minutes, 0 or more")
        self.stale_minutes: int = stale

        form = document.get("form", "event")
        if form == "flat":
            raise MappingError(
                f"{name}: form: flat is not accepted here. It writes an unclosed start tag, which is not an "
                "XML document and cannot be checked against the CoT schema; use `event` (the default)"
            )
        if form != "event":
            raise MappingError(f"{name}: form: `event` is the only form")

        # -- the gateway's own keys --------------------------------------------
        when = document.get("time", "capture")
        if when not in TIME_SOURCES:
            raise MappingError(f"{name}: time: one of {list(TIME_SOURCES)}")
        self.time_source: str = when

        self.track_name = Value(document["name"], f"{name}: name") if "name" in document else None

        motion = document.get("motion")
        self.course_field: str | None = None
        self.speed_field: str | None = None
        self.east_field: str | None = None
        self.north_field: str | None = None
        if motion is not None:
            if (
                not isinstance(motion, dict)
                or not {"course", "speed"} <= set(motion) <= {"course", "speed", "east", "north"}
                or ("east" in motion) != ("north" in motion)
                or not all(isinstance(v, str) and v for v in motion.values())
            ):
                raise MappingError(
                    f"{name}: motion: give course (degrees from true north) and speed (metres per second), "
                    "and optionally east and north together (metres per second), each a field name"
                )
            self.course_field, self.speed_field = motion["course"], motion["speed"]
            self.east_field, self.north_field = motion.get("east"), motion.get("north")

        optional = document.get("optional", [])
        if not isinstance(optional, list) or not all(isinstance(f, str) and f for f in optional):
            raise MappingError(f"{name}: optional: a list of field names")
        allowed = set(self.position_fields[2:] if self.frame == "geodetic" else []) | {
            f for f in (self.course_field, self.speed_field, self.east_field, self.north_field) if f
        }
        if set(optional) - allowed:
            raise MappingError(
                f"{name}: optional: {sorted(set(optional) - allowed)[0]!r} is not the altitude or a motion field; "
                "only those may be absent"
            )
        self.optional: frozenset[str] = frozenset(optional)

        # -- the adapters' own blocks, checked by each adapter -------------------
        self.blocks: dict[str, Any] = {key: document[key] for key in block_names if key in document}
        self.parsed: dict[str, Any] = {}

    @property
    def topic(self) -> str:
        """`WX/Position`: the form first-bite's `topics` lists use."""
        return f"{self.match['subsystem']}/{self.match['message_type']}"

    def matches(self, record: dict[str, Any]) -> bool:
        return all(record.get(key) == wanted for key, wanted in self.match.items())

    def _number_or_none(self, record: dict[str, Any], field: str) -> float | None:
        """A field the mapping lists under `optional` may be absent or null."""
        if field in self.optional and message(record).get(field) is None:
            return None
        return field_number(record, field)

    def track(self, record: dict[str, Any], received_ns: int | None = None) -> Track:
        """The track for `record`. Raises RecordError naming the field that
        is missing or unusable."""
        uid = "_".join(field_text(record, f) for f in self.uid)
        if self.type_constant is not None:
            cot_type = self.type_constant
        else:
            assert self.type_field is not None
            # As first-bite's cot transform: a missing field is an error; a
            # value not listed gives the default.
            cot_type = self.type_values.get(field_text(record, self.type_field), self.type_default)

        if self.frame == "geodetic":
            latitude = field_number(record, self.position_fields[0])
            longitude = field_number(record, self.position_fields[1])
            hae = self._number_or_none(record, self.position_fields[2]) if len(self.position_fields) == 3 else None
        else:
            from .geodesy import ecef_to_geodetic

            latitude, longitude, hae = ecef_to_geodetic(*(field_number(record, f) for f in self.position_fields))
        if not -90 <= latitude <= 90:
            raise RecordError(f"message field {self.position_fields[0]!r} is not a latitude: {latitude}")
        if not -180 <= longitude <= 180:
            raise RecordError(f"message field {self.position_fields[1]!r} is not a longitude: {longitude}")

        if self.time_source == "capture":
            when = capture_ns(record)
        else:
            when = received_ns if received_ns is not None else time.time_ns()

        course = speed = None
        if self.course_field and self.speed_field:
            course_value = self._number_or_none(record, self.course_field)
            speed = self._number_or_none(record, self.speed_field)
            if course_value is None or speed is None:
                course = speed = None
            else:
                course = course_value % 360.0
        if speed is not None:
            if speed < 0:
                raise RecordError(f"message field {self.speed_field!r} is a negative speed: {speed}")
        velocity = None
        if speed is not None and self.east_field and self.north_field:
            east = self._number_or_none(record, self.east_field)
            north = self._number_or_none(record, self.north_field)
            if east is not None and north is not None:
                velocity = (east, north)

        return Track(
            uid=uid,
            cot_type=cot_type,
            latitude=latitude,
            longitude=longitude,
            hae=hae,
            time_ns=when,
            stale_ns=when + self.stale_minutes * 60 * NANOS,
            name=self.track_name.resolve(record) if self.track_name else None,
            course=course,
            speed=speed,
            velocity=velocity,
            source=Source(
                subsystem=str(record.get("subsystem")),
                message_type=str(record.get("message_type")),
                dictionary=record.get("dictionary"),
                dictionary_version=record.get("dictionary_version"),
            ),
        )


def load_mapping(path: Path, blocks: Iterable[str] = ()) -> Mapping:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MappingError(f"{path}: {error}") from None
    return Mapping(document, name=str(path), blocks=blocks)


def load_mappings(paths: Iterable[Path], blocks: Iterable[str] = ()) -> list[Mapping]:
    """Load mapping files; a directory gives every `*.yml` and `*.yaml` in it."""
    blocks = list(blocks)
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted([*path.glob("*.yml"), *path.glob("*.yaml")]))
        else:
            files.append(path)
    if not files:
        raise MappingError("no mapping file given")
    mappings = [load_mapping(path, blocks) for path in files]
    seen: dict[tuple[tuple[str, str], ...], str] = {}
    for mapping in mappings:
        key = tuple(sorted(mapping.match.items()))
        if key in seen:
            raise MappingError(f"{mapping.name} and {seen[key]} match the same records")
        seen[key] = mapping.name
    return mappings


def select(mappings: list[Mapping], record: dict[str, Any]) -> Mapping | None:
    """The mapping that matches `record`, or none."""
    return next((mapping for mapping in mappings if mapping.matches(record)), None)
