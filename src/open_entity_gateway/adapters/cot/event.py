"""A Cursor-on-Target 2.0 event for one track.

The event is the one first-bite's cot transform writes (`form: event`,
cotify/src/event.rs): the same attributes in the same order, numbers written
the same way, `how="m-g"`, `ce` and `le` the CoT value for an unknown error.
Times are written in one of two forms (`time_form`): `z` (the default),
UTC with `Z` and at most microseconds, which OpenTAKServer's parser takes; or
`first-bite`, `+00:00` and up to nanoseconds, as first-bite writes them, which
OpenTAKServer's parser refuses (measured, docs/adapters/cot.md). Both are
valid against the schema. Three things are added:

- `access`, which the MITRE base-event schema published in atak-civ requires
  (its clarification 4, 01 May 23, after MIL-STD-6090). The destination's
  owner sets the value; the default is `Undefined`, the schema's own term.
- `<contact callsign>` inside `<detail>` when the mapping has a `name`.
- `<track course speed>` inside `<detail>` when the mapping has `motion`.

The last two are MITRE public-release detail sub-schemas. Without them, with
`access` removed and `time_form: first-bite`, the event is first-bite's byte for byte
(tests/test_cot.py checks this against vectors cotify wrote).
"""

from __future__ import annotations

from ...mapping import Track
from ...record import decimal_text, rfc3339_offset, rfc3339_z_micro

# How `time`, `start` and `stale` are written.
TIME_FORMS = {
    # UTC with Z, at most microseconds: what OpenTAKServer's parser takes.
    "z": rfc3339_z_micro,
    # As first-bite's cot transform writes them: +00:00, up to nanoseconds.
    "first-bite": rfc3339_offset,
}

# cotify: UNKNOWN_ERROR. Also written for `hae` when the mapping gives no altitude.
UNKNOWN = "9999999"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'
# The values the base-event schema's documentation of `access` lists.
ACCESS_VALUES = (
    "Unclassified",
    "NATO Unclassified",
    "NATO Restricted",
    "Confidential",
    "NATO Confidential",
    "Secret",
    "NATO Secret",
    "Undefined",
)

_ESCAPES = {"<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&apos;", '"': "&quot;"}


def escape(text: str) -> str:
    """Attribute text escaped as quick-xml escapes it (first-bite's writer)."""
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def event_xml(track: Track, access: str | None = None, time_form: str = "first-bite") -> str:
    write = TIME_FORMS[time_form]
    time = write(track.time_ns)
    stale = write(track.stale_ns)
    hae = decimal_text(track.hae) if track.hae is not None else UNKNOWN
    head = (
        f'<event version="2.0" uid="{escape(track.uid)}" type="{escape(track.cot_type)}"'
        f' time="{time}" start="{time}" stale="{stale}" how="m-g"'
        + (f' access="{escape(access)}"' if access is not None else "")
        + ">"
        f'<point lat="{decimal_text(track.latitude)}" lon="{decimal_text(track.longitude)}"'
        f' hae="{hae}" ce="{UNKNOWN}" le="{UNKNOWN}"/>'
    )
    detail = ""
    if track.name is not None:
        detail += f'<contact callsign="{escape(track.name)}"/>'
    if track.course is not None and track.speed is not None:
        detail += f'<track course="{decimal_text(track.course)}" speed="{decimal_text(track.speed)}"/>'
    return head + (f"<detail>{detail}</detail>" if detail else "<detail/>") + "</event>"
