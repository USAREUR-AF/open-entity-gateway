"""The `geojson` wire: a GeoJSON Feature with a Point geometry.

Parsing uses the standard library `json` module with hooks that keep every
number as its raw text (`Num`) and refuse NaN/Infinity. A member name given
twice is accepted here and refused on the `json` wire.
Serialisation is the canonical writer below: members `type`, `geometry`
{`type`, `coordinates`}, `properties` (field order, then residue), then other
top-level members (field order, then residue); no insignificant whitespace;
numbers shortest round-trip.
"""

from .errors import MalformedWire
from .jsonio import (  # noqa: F401  (the JSON text the geojson wire shares with json; not json itself)
    MISSING, Num, RawJSON, canonical_scalar, dump, dump_as_written, dump_string, is_absent, item_text, parse_json,
    sentinel_text, when_matches, when_text,
)
from .packdef import parse_geojson_locator


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


def encode(pack, enc, residue):
    """The canonical Feature of the encoded top scope and the entity's
    residue. A `when` or `const` value is typed as on `json` (0.5 ruling 11);
    a list is an array whose items are written by `item_text` (as on the
    `json` wire, ruling 7 of 0.7; `jsonio` holds it, so no `json` codec is
    needed, ruling 1d of 0.24)."""
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
    sentinels = enc.sentinels
    for i, ln in plan.ifields:
        if i in values and ln.locator not in resolved and i not in sentinels:
            out = values[i]
            if getattr(ln.transform, "exact", False):  # `iscale`: its exact digits (ruling 7 of 0.15)
                resolved[ln.locator] = RawJSON(out)
            else:
                resolved[ln.locator] = Num(out) if ln.transform.kind == "number" else out
    for i, ln in plan.ifields:  # a sentinel where no field with a value writes (ruling 16 of 0.9)
        if i in sentinels and ln.locator not in resolved:
            resolved[ln.locator] = RawJSON(values[i])
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if children is not None and g.locator not in resolved:
            resolved[g.locator] = RawJSON("[" + ",".join([item_text(pack, c) for c in children]) + "]")
    out = [(loc, resolved[loc]) for loc in plan.order if loc in resolved]
    out.extend((loc, val) for loc, val in residue.items() if loc not in resolved)
    return write(pack, out)
