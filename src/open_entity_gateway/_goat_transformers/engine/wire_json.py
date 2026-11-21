"""The `json` wire: one JSON object, members addressed by path.

A path is one or more member names separated by `.`; each name but the last
addresses a nested object (spec/engine-spec.md, "The `json` wire"). Parsing uses
the standard library `json` module with the hooks of `jsonio`, which keep
every number as its raw text and refuse duplicate members and NaN/Infinity.
Serialisation is the canonical writer of `jsonio`.

Residue is every scalar member (string, number, boolean) at any depth of nested
objects that no line maps, keyed by its path, with its canonical JSON text.
Arrays and `null` members are not carried; a member whose name contains `.` is
dropped with everything inside it.
"""

from .errors import MalformedWire
from .jsonio import (  # noqa: F401
    MISSING, Num, canonical_scalar, dump_string, is_absent, item_text, parse_json, resolve, when_matches, when_text,
    write,
)


def parse(pack, data):
    doc = parse_json(data, pack.name)
    if not isinstance(doc, dict):
        raise MalformedWire("top level is not an object", format=pack.name)
    return doc


def lookup(doc, path):
    """The member at `path`, or MISSING. A name that is not reached through
    nested objects is missing."""
    node = doc
    for name in path:
        if not isinstance(node, dict) or name not in node:
            return MISSING
        node = node[name]
    return node


def get(pack, doc, field):
    """The parsed JSON value of one field, or None when it is absent (a member
    whose value is `null` is absent). Inside a group `doc` is one array
    element, and `.` (an empty path) is the element itself."""
    value = lookup(doc, field.loc)
    return None if value is MISSING else value


def items(pack, doc, group):
    """-> the elements of the array at the group's member path, or None when
    the member is absent or `null`. A value that is not an array is
    `malformed-wire`."""
    value = lookup(doc, group.loc)
    if value is MISSING or value is None:
        return None
    if not isinstance(value, list):
        raise MalformedWire(
            "the member of group %s is not an array" % group.list, format=pack.name, locator=group.locator
        )
    return value


def when_holds(pack, doc, when):
    return when_matches(when, lookup(doc, when.loc), pack.name)


def scalar_text(value, fmt=None):
    """Canonical JSON text of a parsed scalar, or None for null, an object or an array."""
    if isinstance(value, Num):
        return canonical_scalar(value, fmt)
    if isinstance(value, str):
        return dump_string(value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return None


def residue(pack, doc, mapped):
    """-> {path: canonical JSON text} of the unmapped scalar members, in input order."""
    out = {}

    def walk(obj, prefix):
        for name, value in obj.items():
            if "." in name:
                continue
            path = prefix + name
            if isinstance(value, dict):
                walk(value, path + ".")
                continue
            if path in mapped:
                continue  # a mapped member is read by its transform, not canonicalised here
            # A residue number whose nearest double is infinite has no
            # canonical text: `malformed-wire` (ruling 2d of 0.11).
            text = scalar_text(value, pack.name)
            if text is not None:
                out[path] = text

    walk(doc, "")
    return out


def encode(pack, enc, residue):
    """The canonical text of the encoded top scope and the entity's residue."""
    resolved = resolve(pack, enc)
    return write(pack, enc.plan.order, resolved, [(loc, val) for loc, val in residue.items() if loc not in resolved])
