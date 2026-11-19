"""Encoding through goat-transformers (`encoder: goat-transformers`).

A gateway track is written as a Canonical Track v2 and converted by the
copied goat-transformers Python engine to `cot-xml`, `asterix-cat062` or
`sapient`. The values a track does not carry, or that the gateway decides, go
in as conversion context, with the keys open-billie's goat-transformers option
uses: the track identity as `track.scoped_id`, the CoT type, stale time, `how`
and callsign; for CAT062 SAC, SIC and the track number; for SAPIENT the object id,
the report `id` and the class.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

from .mapping import Track
from .record import NANOS, RecordError, rfc3339_z

DATA = Path(__file__).resolve().parent / "_goat_transformers" / "data"
BINARY = frozenset({"asterix-cat062", "sapient"})
ENCODERS = ("native", "goat-transformers")
_engine: Any = None
_lock = threading.Lock()


def choose(destination: dict[str, Any], label: str, native_only: tuple[str, ...] = ()) -> str:
    """The destination's `encoder`. With `goat-transformers` the packs are
    loaded here, at start, and a key in `native_only` (one only the native
    encoder writes) is refused rather than dropped. Raises ConfigError."""
    from .adapter import ConfigError

    encoder = destination.get("encoder", "native")
    if encoder not in ENCODERS:
        raise ConfigError(f"destination {label!r}: encoder is native or goat-transformers")
    if encoder == "goat-transformers":
        for key in native_only:
            if key in destination:
                raise ConfigError(
                    f"destination {label!r}: {key} is written only by the native encoder; "
                    "goat-transformers has no place for it (docs/goat-transformers-encoder.md)"
                )
        try:
            engine()
        except Exception as error:  # the engine raises one error class per kind of fault
            raise ConfigError(f"destination {label!r}: the goat-transformers packs did not load: {error}") from None
    return str(encoder)


def engine() -> Any:
    global _engine
    with _lock:
        if _engine is None:
            from ._goat_transformers.engine import Engine

            _engine = Engine.from_dir(str(DATA / "packs"), str(DATA / "model"))
        return _engine


def canonical(track: Track, simulated: bool = False) -> str:
    """The track as a Canonical Track v2 JSON object."""
    position: dict[str, Any] = {"latitudeDeg": track.latitude, "longitudeDeg": track.longitude}
    if track.hae is not None:
        position["altitudeM"] = track.hae
    body: dict[str, Any] = {
        "apiVersion": "billie.dev/track-v2",
        "kind": "Track",
        "trackId": track.uid,
        "sourceId": f"{track.source.subsystem}/{track.source.message_type}",
        "trackGeneration": "1",
        "observedAt": rfc3339_z(track.time_ns),
        "position": position,
        "simulated": simulated,
    }
    if track.course is not None and track.speed is not None:
        course = math.radians(track.course)
        body["velocity"] = {"eastMps": track.speed * math.sin(course), "northMps": track.speed * math.cos(course)}
    return json.dumps(body)


def convert(track: Track, to: str, context: dict[str, str], simulated: bool = False) -> bytes:
    try:
        out = engine().convert("canonical-track-v2", to, canonical(track, simulated), context=context)
    except Exception as error:  # the engine raises one error class per kind of refusal
        raise RecordError(f"goat-transformers refused the track for {to}: {error}") from None
    # The engine writes a bits or protobuf format as hex text, a text format as itself.
    return bytes.fromhex(out.decode()) if to in BINARY else bytes(out)


def cot(track: Track) -> bytes:
    context = {
        "track.scoped_id": track.uid,
        "type": track.cot_type,
        "stale_ms": str(track.stale_ns // (NANOS // 1000)),
        "how": "m-g",
    }
    if track.name is not None:
        # The pack writes `callsign` as detail/contact/@callsign, as the native encoder writes the mapping's name.
        context["callsign"] = track.name
    return convert(track, "cot-xml", context)


def cat062(track: Track, sac: int, sic: int, track_number: int, simulated: bool) -> bytes:
    return convert(
        track,
        "asterix-cat062",
        {
            "cat062.sac": str(sac),
            "cat062.sic": str(sic),
            "cat062.track_number": str(track_number),
            "track.confirmed": "1",
        },
        simulated,
    )


def sapient(track: Track, object_id: str, report_id: str | None, classification: str | None) -> bytes:
    """The SapientMessage the pack writes, holding one DetectionReport. The
    adapter keeps only the report: the session sets the message timestamp,
    node id and report id."""
    context = {"sapient.detection_report_object_id": object_id}
    if report_id is not None:
        context["sapient.detection_report_id"] = report_id
    if classification is not None:
        context["track.class"] = classification
    return convert(track, "sapient", context)
