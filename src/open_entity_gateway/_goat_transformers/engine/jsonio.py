"""The JSON text shared by the `json` and the `geojson` wire: the reader
(numbers kept as their raw text, NaN and Infinity refused, nesting bounded),
the canonical writer, the `when` and sentinel texts, and the writer of one
object, which both wires use for the items of a list.

A build carries this module when it has `json` or `geojson` (Version 0.24,
ruling 1d: `geojson` needs nothing of `json`; both need this).
"""

import json
import re

from .errors import MalformedWire
from .transforms import canonical_number, format_float


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
    texts = when.texts  # 0.20: any value of the list
    if isinstance(value, Num):
        return canonical_scalar(value, fmt) in texts
    if isinstance(value, str):
        return value in texts
    if value is True:
        return "true" in texts
    if value is False:
        return "false" in texts
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


def sentinel_text(value):
    """The raw text a sentinel is compared with on `json` and `geojson` (spec
    0.9): a string's text, a number's canonical text through a double
    (ruling 19), `true` or `false`; None for `null`, an object or an array."""
    if isinstance(value, Num):
        try:
            return format_float(float(value))
        except (ValueError, OverflowError):
            return None
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    return None


def is_absent(pack, field, raw):
    text = sentinel_text(raw)
    return text is not None and text in field.absent


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
