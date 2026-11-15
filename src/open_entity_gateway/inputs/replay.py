"""Records from a JSON-lines file, as first-bite's `local` sink writes them
and as first-bite's examples/weather-station.records.jsonl holds them."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import IO, Any

from ..record import NANOS


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
    capture to a display that drops what is stale."""
    offset: int | None = None
    for record in records:
        stamp = record.get("timestamp")
        if isinstance(stamp, dict) and isinstance(stamp.get("secs_since_epoch"), int):
            then = stamp["secs_since_epoch"] * NANOS + int(stamp.get("nanos_since_epoch") or 0)
            if offset is None:
                offset = start_ns - then
            moved = then + offset
            record = {**record, "timestamp": {"secs_since_epoch": moved // NANOS, "nanos_since_epoch": moved % NANOS}}
        yield record


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
