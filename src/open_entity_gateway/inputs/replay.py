"""Records from a JSON-lines file, as first-bite's `local` sink writes them
and as first-bite's examples/weather-station.records.jsonl holds them."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from typing import IO, Any

from ..canonical_track import is_canonical_track, observed_ns
from ..record import NANOS, RecordError


class ReplayError(ValueError):
    pass


def read_records(stream: IO[str], name: str = "-") -> Iterator[dict[str, Any]]:
    for number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            raise ReplayError(f"{name}:{number}: not JSON") from None
        if not isinstance(record, dict):
            raise ReplayError(f"{name}:{number}: a record is a JSON object")
        yield record


def shift_to(records: Iterator[dict[str, Any]], start_ns: int) -> Iterator[dict[str, Any]]:
    """The records with their capture times moved so that the first is
    `start_ns` and the gaps between them are kept. For replaying an old
    capture to a display that drops what is stale. A record's time is its
    `timestamp`; a Canonical Track's is its `observedAt`. An item whose time
    cannot be read is passed on unchanged, for the gateway to refuse."""
    offset: int | None = None
    for record in records:
        then = _capture_ns(record)
        if then is not None:
            if offset is None:
                offset = start_ns - then
            record = _with_capture_ns(record, then + offset)
        yield record


def _capture_ns(record: dict[str, Any]) -> int | None:
    if is_canonical_track(record):
        try:
            return observed_ns(record.get("observedAt"))
        except RecordError:
            return None
    stamp = record.get("timestamp")
    if isinstance(stamp, dict) and isinstance(stamp.get("secs_since_epoch"), int):
        return int(stamp["secs_since_epoch"] * NANOS + int(stamp.get("nanos_since_epoch") or 0))
    return None


def _with_capture_ns(record: dict[str, Any], moved: int) -> dict[str, Any]:
    if is_canonical_track(record):
        return {**record, "observedAt": rfc3339(moved)}
    return {**record, "timestamp": {"secs_since_epoch": moved // NANOS, "nanos_since_epoch": moved % NANOS}}


def rfc3339(ns: int) -> str:
    """UTC, `Z`, with the fraction to the nanosecond and at least milliseconds."""
    whole = dt.datetime.fromtimestamp(ns // NANOS, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{ns % NANOS:09d}".rstrip("0").ljust(3, "0")
    return f"{whole}.{fraction}Z"


def paced(records: Iterator[dict[str, Any]], rate: float) -> Iterator[dict[str, Any]]:
    interval = 1 / rate if rate else 0.0
    due = time.monotonic()
    for record in records:
        if interval:
            pause = due - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            due += interval
        yield record
