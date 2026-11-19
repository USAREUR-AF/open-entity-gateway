"""The `geojson` wire: a GeoJSON Feature with a Point geometry.

Parsing uses the standard library `json` module with hooks that keep every
number as its raw text (`Num`) and refuse NaN/Infinity. A member name given
twice is accepted here and refused on the `json` wire.
Serialisation is the canonical writer below: members `type`, `geometry`
{`type`, `coordinates`}, `properties` (field order, then residue), then other
top-level members (field order, then residue); no insignificant whitespace;
numbers shortest round-trip.
"""

import json
import re

from .errors import MalformedWire
from .packdef import parse_geojson_locator
from .transforms import canonical_number


MISSING = object()


class Num(str):
    """A JSON number, held as its raw text."""


class RawJSON(str):
    """A canonical JSON value text, written verbatim (residue of non-strings)."""


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate member %r" % k)
        out[k] = v
    return out


def _const(name):
    raise ValueError("%s is not valid JSON" % name)


MAX_DEPTH = 64  # ruling 35 of 0.8: arrays and objects, the outermost is level 1

# A whole string, a quote that opens no whole string, or one bracket outside a
# string.
_STRING_OR_BRACKET = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|"|[\[\]{}]', re.S)
_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F]")
# One escape: a surrogate pair, a `u` escape, or any other escaped character.
_ESCAPE = re.compile(r"\\(?:u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u([0-9a-fA-F]{4})|.)", re.S)


def _check_depth(data, fmt):
    """Refuse an input nested deeper than MAX_DEPTH before it is parsed, in one
    pass that uses no stack."""
    if data.count("[") + data.count("{") <= MAX_DEPTH:
        return
    depth = 0
    for m in _STRING_OR_BRACKET.finditer(data):
        c = data[m.start()]
        if c == "[" or c == "{":
            depth += 1
            if depth > MAX_DEPTH:
                raise MalformedWire("nested deeper than %d levels" % MAX_DEPTH, format=fmt)
        elif c == '"':
            if m.end() - m.start() == 1:
                raise MalformedWire("not JSON: unterminated string at offset %d" % m.start(), format=fmt)
        elif depth:
            depth -= 1


def _check_surrogates(data, fmt):
    """An escape of a surrogate that is not half of a pair names no character,
    and no UTF-8 output can hold it: `malformed-wire`."""
    for m in _ESCAPE.finditer(data):
        code = m.group(1)
        if code is not None and 0xD800 <= int(code, 16) <= 0xDFFF:
            raise MalformedWire("not JSON: unpaired surrogate escape u%s" % code, format=fmt)


def parse_json(data, fmt, duplicates=False):
    """`duplicates`: accept a member name given twice in one object (the last
    value stands). The `json` wire refuses it (0.5 ruling 22); `geojson` keeps
    its 0.1 behaviour and accepts it (0.5 ruling 28)."""
    _check_depth(data, fmt)
    if _SURROGATE_ESCAPE.search(data) is not None:
        _check_surrogates(data, fmt)
    try:
        return json.loads(
            data, parse_float=Num, parse_int=Num, parse_constant=_const,
            object_pairs_hook=dict if duplicates else _pairs,
        )
    except ValueError as e:
        raise MalformedWire("not JSON: %s" % e, format=fmt)
    except RecursionError:  # not reached: the nesting is checked above
        raise MalformedWire("not JSON: nested too deeply", format=fmt)


def dump(v):
    """Canonical JSON text of a parsed value."""
    if isinstance(v, RawJSON):
        return str(v)
    if isinstance(v, Num):
        return canonical_number(str(v))
    if isinstance(v, str):
        return dump_string(v)
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, dict):
        return "{" + ",".join(dump_string(k) + ":" + dump(x) for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(dump(x) for x in v) + "]"
    raise TypeError("cannot write %r as JSON" % (v,))


def dump_as_written(v):
    """JSON text of a parsed array or object that comes back as it was (0.6
    ruling 13): a number keeps its text as written; no insignificant
    whitespace."""
    if isinstance(v, Num):
        return str(v)
    if isinstance(v, dict):
        return "{" + ",".join(dump_string(k) + ":" + dump_as_written(x) for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(dump_as_written(x) for x in v) + "]"
    return dump(v)


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def dump_string(s):
    out = ['"']
    for ch in s:
        e = _ESC.get(ch)
        if e is not None:
            out.append(e)
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


def canonical_scalar(v, fmt=None):
    """Canonical JSON text of a parsed number, or None for any other value."""
    try:
        return canonical_number(str(v))
    except (ValueError, OverflowError):
        raise MalformedWire("number %s is out of range" % v, format=fmt)


def when_matches(when, value, fmt=None):
    """Whether a parsed JSON member value satisfies a `when` (spec 0.5): a string
    equal to the text, or a number or boolean whose canonical text equals it;
    `present`: any value but `null`. `value` is MISSING when the member is absent."""
    if value is MISSING or value is None:
        return False
    if when.present:
        return True
    if isinstance(value, Num):
        return canonical_scalar(value, fmt) == when.value
    if isinstance(value, str):
        return value == when.value
    if value is True:
        return when.value == "true"
    if value is False:
        return when.value == "false"
    return False


def when_text(when):
    """The JSON text a `when` value is written with (0.5 ruling 11): canonical
    JSON number text as a number, `true` / `false` as booleans, anything else,
    and any value written with a leading `=`, as a string."""
    v = when.value
    if not when.forced:
        if v in ("true", "false"):
            return v
        if _NUMBER.match(v):
            try:
                if canonical_number(v) == v:
                    return v
            except (ValueError, OverflowError):
                pass
    return dump_string(v)


def when_holds(pack, first, when):
    return when_matches(when, first.get(when.locator, MISSING), pack.name)


class Top(dict):
    """The Feature as {locator: JSON value}, the first value of each locator."""


NULL = object()  # a top-level member whose value is `null`: present, and no transform reads it


def get(pack, ctx, field):
    """The JSON value of one field, or None when it is absent. At the top a
    `null` member is present (0.1 behaviour, kept by 0.5 ruling 28); inside a
    group the item is addressed as on `json` and `null` is absent (ruling 7 of
    0.7)."""
    if type(ctx) is Top:
        value = ctx.get(field.locator, MISSING)
        return None if value is MISSING else NULL if value is None else value
    value = _lookup(ctx, field.loc)
    return None if value is MISSING else value


def _lookup(node, path):
    for name in path:
        if not isinstance(node, dict) or name not in node:
            return MISSING
        node = node[name]
    return node


def items(pack, ctx, group):
    """-> the elements of the array at the group's member, or None when the
    member is absent or `null`. A value that is not an array is
    `malformed-wire`."""
    value = ctx.get(group.locator, MISSING) if type(ctx) is Top else _lookup(ctx, group.loc)
    if value is MISSING or value is None:
        return None
    if not isinstance(value, list):
        raise MalformedWire(
            "the member of group %s is not an array" % group.list, format=pack.name, locator=group.locator
        )
    return value


def read(pack, data):
    """-> list of (locator, JSON value) observations."""
    fmt = pack.name
    doc = parse_json(data, fmt, duplicates=True)
    if not isinstance(doc, dict):
        raise MalformedWire("top level is not an object", format=fmt)
    if doc.get("type") != "Feature":
        raise MalformedWire("type is not \"Feature\"", format=fmt, locator="type")
    obs = []
    geom = doc.get("geometry")
    if geom is not None:
        if not isinstance(geom, dict):
            raise MalformedWire("geometry is not an object", format=fmt, locator="geometry")
        if geom.get("type") != "Point":
            raise MalformedWire("geometry type is not \"Point\"", format=fmt, locator="geometry.type")
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or not 2 <= len(coords) <= 3:
            raise MalformedWire(
                "Point coordinates must be [lon, lat] or [lon, lat, alt]", format=fmt,
                locator="geometry.coordinates",
            )
        for slot, v in zip(("lon", "lat", "alt"), coords):
            if not isinstance(v, Num):
                raise MalformedWire("coordinate is not a number", format=fmt, locator="geometry." + slot)
            obs.append(("geometry." + slot, v))
        for k, v in geom.items():
            if k not in ("type", "coordinates"):
                obs.append(("geometry." + k, v))
    props = doc.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            raise MalformedWire("properties is not an object", format=fmt, locator="properties")
        for k, v in props.items():
            obs.append(("properties." + k, v))
    for k, v in doc.items():
        if k not in ("type", "geometry", "properties"):
            obs.append((k, v))
    return obs


def write(pack, items):
    """items: iterable of (locator, JSON value) in write order; first value for a
    locator wins. Values are str (string), Num (number) or RawJSON."""
    coords = [None, None, None]
    geom_extra = {}
    props = {}
    top = {}
    for locator, value in items:
        if locator.startswith("geometry."):
            slot = locator[len("geometry."):]
            idx = {"lon": 0, "lat": 1, "alt": 2}.get(slot)
            if idx is not None:
                if coords[idx] is None:
                    coords[idx] = value
            elif slot not in ("type", "coordinates"):
                geom_extra.setdefault(slot, value)
            continue
        try:
            kind, name = parse_geojson_locator(locator)
        except ValueError:
            continue
        if kind == "prop":
            props.setdefault(name, value)
        elif kind == "top":
            top.setdefault(name, value)
    parts = ['"type":"Feature"']
    if coords[0] is not None and coords[1] is not None:
        c = coords if coords[2] is not None else coords[:2]
        g = '{"type":"Point","coordinates":[' + ",".join(dump(x) for x in c) + "]"
        for k, v in geom_extra.items():
            g += "," + dump_string(k) + ":" + dump(v)
        parts.append('"geometry":' + g + "}")
    else:
        parts.append('"geometry":null')
    parts.append('"properties":{' + ",".join(dump_string(k) + ":" + dump(v) for k, v in props.items()) + "}")
    for k, v in top.items():
        parts.append(dump_string(k) + ":" + dump(v))
    return "{" + ",".join(parts) + "}"


def encode(pack, enc, residue, item_text):
    """The canonical Feature of the encoded top scope and the entity's
    residue. A `when` or `const` value is typed as on `json` (0.5 ruling 11);
    a list is an array whose items are written by `item_text` (the `json`
    wire's, ruling 7 of 0.7)."""
    plan = enc.plan
    resolved = {}
    for ln in plan.lines:
        if ln.is_field or ln.is_group or ln.present or ln.locator in resolved:
            continue
        text = when_text(ln)
        resolved[ln.locator] = (
            ln.value if text.startswith('"') else Num(text) if text not in ("true", "false") else RawJSON(text)
        )
    values = enc.values
    for i, ln in plan.ifields:
        if i in values and ln.locator not in resolved:
            out = values[i]
            resolved[ln.locator] = Num(out) if ln.transform.kind == "number" else out
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if children is not None and g.locator not in resolved:
            resolved[g.locator] = RawJSON("[" + ",".join([item_text(pack, c) for c in children]) + "]")
    out = [(loc, resolved[loc]) for loc in plan.order if loc in resolved]
    out.extend((loc, val) for loc, val in residue.items() if loc not in resolved)
    return write(pack, out)
