"""Encoding through goat-transformers (`encoder: goat-transformers`).

A gateway track is written as a Canonical Track v2 and converted by the
copied goat-transformers Python engine to `cot-xml` or `asterix-cat062`. The
values a track does not carry, or that the gateway decides, go in as
conversion context, with the keys open-billie's goat-transformers option uses:
the track identity as `track.scoped_id`, the CoT type, stale time and `how`,
and for CAT062 SAC, SIC and the track number.
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
_engine: Any = None
_lock = threading.Lock()


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
    return convert(
        track,
        "cot-xml",
        {
            "track.scoped_id": track.uid,
            "type": track.cot_type,
            "stale_ms": str(track.stale_ns // (NANOS // 1000)),
            "how": "m-g",
        },
    )


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
