"""Reading a first-bite record.

A record is the JSON object first-bite publishes for one captured packet
(first-bite docs/record.md). Its `message` member holds the decoded fields.
Version 2 records carry typed values; version 1 records carry every value as
text. Both are read here the same way, so a mapping works on either.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

# Members of the record itself, outside `message`, that a mapping may read.
RECORD_MEMBERS = frozenset(
    {
        "subsystem",
        "message_type",
        "dictionary",
        "dictionary_version",
        "capture_interface",
        "application_version",
        "data_source_network_name",
        "data_source_owner",
        "data_source_system_type",
    }
)

NANOS = 1_000_000_000


class RecordError(ValueError):
    """A record lacks a value its mapping needs. The message names the field."""


def message(record: dict[str, Any]) -> dict[str, Any]:
    body = record.get("message")
    if not isinstance(body, dict):
        raise RecordError("record carries no decoded message (member 'message')")
    return body


def json_text(value: Any) -> str:
    """A JSON number or boolean as text, written as first-bite's cot transform
    writes it (serde_json): `true`, `7`, `136.5`, `1e-7`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = repr(float(value))
    if "e" in text:
        mantissa, exponent = text.split("e")
        text = f"{mantissa}e{int(exponent)}"
    return text


def field_text(record: dict[str, Any], field: str) -> str:
    """The value of a message field as text: the label or number as decoded."""
    value = message(record).get(field)
    if value is None:
        raise RecordError(f"message field {field!r} is missing or null")
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return json_text(value)
    raise RecordError(f"message field {field!r} is not a single value")


def field_number(record: dict[str, Any], field: str) -> float:
    """A message field as a finite number: a JSON number, or a decimal string
    as a version 1 record carries it."""
    text = field_text(record, field)
    try:
        number = float(text.strip())
    except ValueError:
        raise RecordError(f"message field {field!r} is not a number: {text!r}") from None
    if not math.isfinite(number):
        raise RecordError(f"message field {field!r} is not a finite number: {text!r}")
    return number


def member_text(record: dict[str, Any], member: str) -> str:
    value = record.get(member)
    if value is None or value == "":
        raise RecordError(f"record member {member!r} is missing or empty")
    if isinstance(value, (dict, list)):
        raise RecordError(f"record member {member!r} is not a single value")
    return value if isinstance(value, str) else json_text(value)


def capture_ns(record: dict[str, Any]) -> int:
    """The record's capture time, in nanoseconds since 1970-01-01T00:00:00Z."""
    stamp = record.get("timestamp")
    try:
        seconds = stamp["secs_since_epoch"]  # type: ignore[index]
        nanos = stamp["nanos_since_epoch"]  # type: ignore[index]
        if isinstance(seconds, bool) or isinstance(nanos, bool):
            raise TypeError
        seconds, nanos = int(seconds), int(nanos)
    except (TypeError, KeyError, ValueError):
        raise RecordError("record member 'timestamp' is missing or not a capture time") from None
    if not 0 <= nanos < NANOS or seconds < 0:
        raise RecordError("record member 'timestamp' is out of range")
    return seconds * NANOS + nanos


def rfc3339_offset(ns: int) -> str:
    """`2026-11-15T15:56:11.709165450+00:00`: the form first-bite's cot
    transform writes (chrono's to_rfc3339): 0, 3, 6 or 9 fractional digits,
    as few as keep the value."""
    seconds, nanos = divmod(ns, NANOS)
    text = dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if nanos:
        if nanos % 1_000_000 == 0:
            text += f".{nanos // 1_000_000:03d}"
        elif nanos % 1_000 == 0:
            text += f".{nanos // 1_000:06d}"
        else:
            text += f".{nanos:09d}"
    return text + "+00:00"


def rfc3339_z(ns: int) -> str:
    """`2025-06-15T15:06:43Z`, with as many fractional digits as needed."""
    seconds, nanos = divmod(ns, NANOS)
    text = dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if nanos:
        text += f".{nanos:09d}".rstrip("0")
    return text + "Z"


def rfc3339_z_micro(ns: int) -> str:
    """`2026-11-15T20:25:44.96229Z`: UTC with `Z`, at most six fractional
    digits (truncated). The form OpenTAKServer's parser takes (measured)."""
    seconds, nanos = divmod(ns, NANOS)
    text = dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    micros = nanos // 1000
    if micros:
        text += f".{micros:06d}".rstrip("0")
    return text + "Z"


def decimal_text(number: float) -> str:
    """A float written as Rust writes an f64 (`Display`): the shortest digits
    that keep the value, never an exponent. `136.5`, `100`, `0.0000001`."""
    if number == 0:
        return "-0" if math.copysign(1.0, number) < 0 else "0"
    text = format(Decimal(repr(number)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
