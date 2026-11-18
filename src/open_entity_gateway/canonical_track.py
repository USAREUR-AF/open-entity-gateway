"""Canonical Track v2 as input.

A Canonical Track v2 object (`apiVersion: billie.dev/track-v2`, `kind: Track`;
open-billie `schemas/canonical-track-v2.schema.json`, the goat-transformers
pack `canonical-track-v2`) is turned into a record, so that the mapping file
`mappings/canonical-track-v2.yml` makes it a track like any other record. The
record keeps every value the track carries; nothing is guessed. Velocity east
and north become course and speed, the two motion fields a mapping reads.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

from .record import NANOS, RecordError

API_VERSION = "billie.dev/track-v2"
KIND = "Track"
SUBSYSTEM, MESSAGE_TYPE, DICTIONARY = "TRACK", "Track", "canonical-track-v2"

_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")


def is_canonical_track(value: Any) -> bool:
    return isinstance(value, dict) and value.get("apiVersion") == API_VERSION and value.get("kind") == KIND


def _observed_ns(text: Any) -> int:
    match = _TIME.match(text) if isinstance(text, str) else None
    if match is None:
        raise RecordError(f"track member 'observedAt' is not an RFC 3339 time: {text!r}")
    whole, fraction, zone = match.groups()
    when = dt.datetime.fromisoformat(whole + ("+00:00" if zone == "Z" else zone))
    nanos = int((fraction or "").ljust(9, "0"))
    return int(when.timestamp()) * NANOS + nanos


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RecordError(f"track member {where!r} is not a finite number: {value!r}")
    return float(value)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecordError(f"track member {where!r} is missing or empty")
    return value


def track_record(track: dict[str, Any]) -> dict[str, Any]:
    """The record for one Canonical Track v2 object. Raises RecordError naming
    the member that is missing or unusable."""
    position = track.get("position")
    if not isinstance(position, dict):
        raise RecordError("track member 'position' is missing")
    generation = track.get("trackGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise RecordError(f"track member 'trackGeneration' is not a whole number: {generation!r}")
    observed = _observed_ns(track.get("observedAt"))

    body: dict[str, Any] = {
        "track_id": _text(track.get("trackId"), "trackId"),
        "source_id": _text(track.get("sourceId"), "sourceId"),
        "generation": generation,
        "latitude": _number(position.get("latitudeDeg"), "position.latitudeDeg"),
        "longitude": _number(position.get("longitudeDeg"), "position.longitudeDeg"),
        "classification": track.get("classification") or "unknown",
    }
    if position.get("altitudeM") is not None:
        body["altitude"] = _number(position["altitudeM"], "position.altitudeM")

    velocity = track.get("velocity")
    if isinstance(velocity, dict) and velocity.get("eastMps") is not None and velocity.get("northMps") is not None:
        east = _number(velocity["eastMps"], "velocity.eastMps")
        north = _number(velocity["northMps"], "velocity.northMps")
        body["speed"] = math.hypot(east, north)
        body["course"] = math.degrees(math.atan2(east, north)) % 360.0

    for member, key in (("confidence", "confidence"), ("simulated", "simulated"), ("testData", "test_data")):
        if track.get(member) is not None:
            body[key] = track[member]

    return {
        "subsystem": SUBSYSTEM,
        "message_type": MESSAGE_TYPE,
        "dictionary": DICTIONARY,
        "dictionary_version": API_VERSION,
        "timestamp": {"secs_since_epoch": observed // NANOS, "nanos_since_epoch": observed % NANOS},
        "message": body,
    }
