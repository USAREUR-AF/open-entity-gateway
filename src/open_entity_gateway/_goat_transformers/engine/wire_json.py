"""The `json` wire: one JSON object, members addressed by path.

A path is one or more member names separated by `.`; each name but the last
addresses a nested object (spec/engine-spec.md, "The `json` wire"). Parsing uses
the standard library `json` module with the hooks of the geojson wire, which
keep every number as its raw text and refuse duplicate members and
NaN/Infinity. Serialisation is the canonical writer below.

Residue is every scalar member (string, number, boolean) at any depth of nested
objects that no line maps, keyed by its path, with its canonical JSON text.
Arrays and `null` members are not carried; a member whose name contains `.` is
dropped with everything inside it.
"""

from .errors import MalformedWire
from .wire_geojson import (
    MISSING, Num, canonical_scalar, dump_string, is_absent, parse_json, when_matches, when_text,  # noqa: F401
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
                continue
            # A residue number whose nearest double is infinite has no
            # canonical text: `malformed-wire` (ruling 2d of 0.11).
            text = scalar_text(value, pack.name)
            if text is not None:
                out[path] = text

    walk(doc, "")
    return out


class _Obj:
    """A member being written: a scalar's JSON text, or the members of an object."""

    __slots__ = ("text", "members")

    def __init__(self):
        self.text = None
        self.members = {}


def _slot(root, path):
    node = root
    for name in path:
        child = node.members.get(name)
        if child is None:
            child = node.members[name] = _Obj()
        node = child
    return node


def _ser(node):
    """-> JSON text, or None when the member is not written (an absent scalar,
    or an object that would have no members)."""
    if node.text is not None:
        return node.text
    parts = []
    for name, child in node.members.items():
        text = _ser(child)
        if text is not None:
            parts.append(dump_string(name) + ":" + text)
    return "{" + ",".join(parts) + "}" if parts else None


def write(pack, order, resolved, residue_items):
    """`order` is every line's locator in line order (common lines first), each
    once; `resolved` maps a locator to the JSON text written there. A member, and
    each nested object on its path, takes the position of the first line that
    addresses it, whether or not that line writes a value. Residue members
    follow in input order. An object with no members is not written; the
    message itself is always an object."""
    root = _Obj()
    for locator in order:
        slot = _slot(root, locator.split("."))
        if locator in resolved and slot.text is None:
            slot.text = resolved[locator]
    for locator, text in residue_items:
        slot = _slot(root, locator.split("."))
        if slot.text is None and not slot.members:
            slot.text = text
    return _ser(root) or "{}"


def resolve(pack, enc):
    """-> {locator: JSON text} of one encoded scope. A locator's value is a
    `when` or `const` value if such a line names it, otherwise that of the
    earliest line whose key has a value. A list is an array of its items; an
    empty list is `[]`."""
    plan = enc.plan
    lines = plan.lines
    resolved = {}
    for ln in lines:
        if ln.is_field or ln.is_group or ln.present or ln.locator in resolved:
            continue
        resolved[ln.locator] = when_text(ln)
    values = enc.values
    sentinels = enc.sentinels
    for i, ln in plan.ifields:
        if i in values and ln.locator not in resolved and i not in sentinels:
            out = values[i]
            resolved[ln.locator] = dump_string(out) if ln.transform.kind == "string" else out
    for i, ln in plan.ifields:  # a sentinel where no field with a value writes (ruling 16 of 0.9)
        if i in sentinels and ln.locator not in resolved:
            resolved[ln.locator] = values[i]
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if children is not None and g.locator not in resolved:
            resolved[g.locator] = "[" + ",".join([item_text(pack, c) for c in children]) + "]"
    return resolved


def item_text(pack, enc):
    """The JSON text of one item: the scalar of its `.` field (`null` when the
    item has no value for it), otherwise an object, `{}` when no member is
    written."""
    resolved = resolve(pack, enc)
    plan = enc.plan
    if "." in plan.mapped:
        return resolved.get(".", "null")
    return write(pack, plan.order, resolved, ())


def encode(pack, enc, residue):
    """The canonical text of the encoded top scope and the entity's residue."""
    resolved = resolve(pack, enc)
    return write(pack, enc.plan.order, resolved, [(loc, val) for loc, val in residue.items() if loc not in resolved])
