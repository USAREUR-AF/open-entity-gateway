"""The `protobuf` wire: a protobuf message addressed by field number.

Input and output are hex text (spec/engine-spec.md, "Version 0.3: the protobuf
wire"). A field is addressed by its number; a dot-path addresses a field inside a
nested length-delimited submessage (`4.1` is field 1 of the submessage in field
4). When a field number repeats on the wire, the last occurrence wins.

`get` resolves a field's dot-path to one `(wire_type, raw)` observation, which
the field's `p*` transform then interprets. For a varint the
raw value is the decoded integer; for fixed64/fixed32 it is the raw bytes; for a
length-delimited field it is the content bytes. `write` lays the transforms'
encoded `(wire_type, payload)` back into a canonical message: fields in ascending
field-number order, each submessage serialised the same way, only mapped fields
written.
"""

from .errors import MalformedWire
from .wire_bits import _hex_to_bytes

VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5
MAX_FIELD_NUMBER = (1 << 29) - 1  # ruling 38 of 0.8




def _read_varint(buf, i, fmt):
    """-> (value, next_index). A varint is at most ten bytes and holds 64
    bits: a tenth byte greater than 1 is `malformed-wire` (ruling 40 of 0.8)."""
    result = 0
    shift = 0
    for _ in range(10):
        if i >= len(buf):
            raise MalformedWire("truncated varint", format=fmt)
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            if shift == 63 and b > 1:
                raise MalformedWire("varint holds more than 64 bits", format=fmt)
            return result, i
        shift += 7
    raise MalformedWire("varint longer than ten bytes", format=fmt)


def _encode_varint(value):
    """Unsigned varint bytes; a negative integer is taken as its 64-bit two's
    complement (protobuf's int64 encoding)."""
    v = value & ((1 << 64) - 1) if value < 0 else value
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_num, wire_type):
    return _encode_varint((field_num << 3) | wire_type)


def parse_message(buf, fmt, locator=None):
    """Parse one message level -> list of (field_number, wire_type, raw) in wire
    order. `raw` is the varint integer (wt 0), the eight bytes (wt 1), the content
    bytes (wt 2) or the four bytes (wt 5). Wire types 3 and 4 (the deprecated
    groups) are a decode error."""
    out = []
    i, n = 0, len(buf)
    while i < n:
        tag, i = _read_varint(buf, i, fmt)
        field_num = tag >> 3
        wt = tag & 7
        if field_num == 0 or field_num > MAX_FIELD_NUMBER:
            raise MalformedWire("field number %d is invalid" % field_num, format=fmt, locator=locator)
        if wt == VARINT:
            val, i = _read_varint(buf, i, fmt)
            out.append((field_num, wt, val))
        elif wt == FIXED64:
            if i + 8 > n:
                raise MalformedWire("truncated fixed64 field", format=fmt, locator=locator)
            out.append((field_num, wt, buf[i:i + 8]))
            i += 8
        elif wt == LEN:
            length, i = _read_varint(buf, i, fmt)
            if i + length > n:
                raise MalformedWire("truncated length-delimited field", format=fmt, locator=locator)
            out.append((field_num, wt, buf[i:i + length]))
            i += length
        elif wt == FIXED32:
            if i + 4 > n:
                raise MalformedWire("truncated fixed32 field", format=fmt, locator=locator)
            out.append((field_num, wt, buf[i:i + 4]))
            i += 4
        else:
            raise MalformedWire(
                "unsupported protobuf wire type %d (groups) at field %d" % (wt, field_num),
                format=fmt, locator=locator,
            )
    return out


class Message:
    """One parsed message level (the top-level message, or one submessage item
    of a group): its fields indexed by number, nested submessages parsed on
    demand."""

    __slots__ = ("fmt", "top", "_occ", "_subs")

    def __init__(self, fmt, top):
        self.fmt = fmt
        self.top = top
        self._occ = None
        self._subs = None

    def occurrences(self):
        """-> {field number: its (wire_type, raw) occurrences, in wire order}."""
        occ = self._occ
        if occ is None:
            occ = self._occ = {}
            for num, wt, raw in self.top:
                same = occ.get(num)
                if same is None:
                    occ[num] = [(wt, raw)]
                else:
                    same.append((wt, raw))
        return occ

    def sub(self, num, locator=None):
        """-> the submessage in the last occurrence of field `num`, or None
        when the field does not occur. A field that is not a submessage is
        `malformed-wire` (ruling 13 of 0.7, ruling 30 of 0.8); `when_holds`
        turns that into "does not hold" for a `when` path (ruling 31)."""
        subs = self._subs
        if subs is None:
            subs = self._subs = {}
        elif num in subs:
            return subs[num]
        same = self.occurrences().get(num)
        if same is None:
            subs[num] = None
            return None
        wt, raw = same[-1]
        if wt != LEN:
            raise MalformedWire(
                "path segment %d of %s is not a submessage (wire type %d)" % (num, locator, wt),
                format=self.fmt, locator=locator,
            )
        sub = subs[num] = Message(self.fmt, parse_message(raw, self.fmt, locator=locator))
        return sub

    def descend(self, path, n, locator=None):
        """-> the submessage reached by the first `n` numbers of `path` (the
        last occurrence of each), or None when it does not occur."""
        msg = self
        for k in range(n):
            msg = msg.sub(path[k], locator)
            if msg is None:
                return None
        return msg

    def lookup(self, path, locator=None):
        """-> (wire_type, raw) of the field at `path` (the last occurrence of
        each number), or None when it does not occur."""
        n = len(path) - 1
        msg = self.descend(path, n, locator) if n else self
        if msg is None:
            return None
        same = msg.occurrences().get(path[n])
        return same[-1] if same is not None else None


class Scalar:
    """One scalar item of a group: the `(wire_type, raw)` of one occurrence, or
    of one value of a packed occurrence."""

    __slots__ = ("leaf",)

    def __init__(self, leaf):
        self.leaf = leaf


def parse(pack, data):
    fmt = pack.name
    return Message(fmt, parse_message(_hex_to_bytes(data, fmt), fmt))


def when_holds(pack, msg, when):
    """`present`: the field at the path occurs on the wire. An integer: the
    field is a varint equal to it. A `when` path whose prefix is not a
    submessage does not hold; it is not an error (ruling 31 of 0.8)."""
    try:
        leaf = msg.lookup(when.loc, locator=when.locator)
    except MalformedWire:
        return False
    if leaf is None:
        return False
    if when.present:
        return True
    return leaf[0] == VARINT and when.holds_int(leaf[1])


def get(pack, msg, field):
    """-> (wire_type, raw) for the field's transform, or None when absent. `.`
    (an empty path) is the item itself, when it is a scalar."""
    if type(msg) is Scalar:
        return msg.leaf if not field.loc else None
    if not field.loc:
        return None
    return msg.lookup(field.loc, locator=field.locator)


def is_absent(pack, field, raw):
    """Whether the `(wire_type, raw)` of a field with `absent` is one of its
    sentinels (spec 0.9): a varint before zig-zag, the fixed bytes as an
    unsigned little-endian integer, the bytes of a `pstring`. A wire type
    other than the transform's is never a sentinel: the transform then fails
    (ruling 15 of 0.9)."""
    wt, value = raw
    if wt != field.transform.wire_type:
        return False
    if wt == FIXED64 or wt == FIXED32:
        value = int.from_bytes(value, "little")
    elif wt == LEN:
        value = bytes(value)
    return value in field.absent


def _scalar_field(group):
    """The `.` field of a group body (its index in the body and the line), or
    None when the items are submessages."""
    cache = group.plan.cache
    if "scalar" not in cache:
        cache["scalar"] = next(((i, f) for i, f in group.plan.ifields if not f.loc), None)
    return cache["scalar"]


def _unpack(raw, wire_type, fmt, locator):
    """The values of a packed occurrence, back to back -> [(wire_type, raw)].
    A payload whose length does not fit its values is `malformed-wire`."""
    out = []
    if wire_type == VARINT:
        i, n = 0, len(raw)
        while i < n:
            val, i = _read_varint(raw, i, fmt)
            out.append((VARINT, val))
        return out
    width = 8 if wire_type == FIXED64 else 4
    if len(raw) % width:
        raise MalformedWire(
            "a packed payload of %d byte(s) does not hold whole %d-byte values" % (len(raw), width),
            format=fmt, locator=locator,
        )
    return [(wire_type, raw[k:k + width]) for k in range(0, len(raw), width)]


def items(pack, msg, group):
    """-> one context per item of `group`, in wire order, or None when no item
    occurs. An item is each occurrence of the last field number of the group's
    path. With a `.` field the items are scalars: a numeric transform also
    accepts the packed form, one length-delimited occurrence holding the
    values back to back. Otherwise each occurrence is read as a submessage; one
    that is not is `malformed-wire` (ruling 8 of 0.7)."""
    if type(msg) is Scalar:
        return None
    path = group.loc
    parent = msg.descend(path, len(path) - 1, group.locator)
    if parent is None:
        return None
    occ = parent.occurrences().get(path[-1])
    if not occ:
        return None
    scalar = _scalar_field(group)
    out = []
    if scalar is not None:
        want = scalar[1].transform.wire_type
        for wt, raw in occ:
            if wt == LEN and want != LEN:
                out.extend(Scalar(leaf) for leaf in _unpack(raw, want, msg.fmt, group.locator))
            else:
                out.append(Scalar((wt, raw)))
        return out or None  # a packed occurrence holding no values leaves the list absent
    for wt, raw in occ:
        if wt != LEN:
            raise MalformedWire(
                "an item of group %s is not a submessage (wire type %d)" % (group.list, wt),
                format=msg.fmt, locator=group.locator,
            )
        out.append(Message(msg.fmt, parse_message(raw, msg.fmt, locator=group.locator)))
    return out


class _Rep(list):
    """The occurrences of a repeated field, in order: `(wire_type, payload)` each."""


def _serialize(tree):
    """Serialise a field-number-keyed tree to bytes in ascending field order. A
    leaf is (wire_type, payload); a dict is a nested submessage."""
    out = bytearray()
    for num in sorted(tree):
        node = tree[num]
        if isinstance(node, dict):
            payload = _serialize(node)
            out += _tag(num, LEN)
            out += _encode_varint(len(payload))
            out += payload
            continue
        for wt, payload in (node if type(node) is _Rep else (node,)):
            out += _tag(num, wt)
            if wt == VARINT:
                out += _encode_varint(payload)
            elif wt == LEN:
                out += _encode_varint(len(payload))
                out += payload
            else:  # FIXED64 / FIXED32: payload is the raw fixed-width bytes
                out += payload
    return out


def _set(tree, path, value, overwrite):
    node = tree
    for num in path[:-1]:
        child = node.get(num)
        if not isinstance(child, dict):
            child = {}
            node[num] = child
        node = child
    if overwrite or path[-1] not in node:
        node[path[-1]] = value


def _has(tree, path):
    node = tree
    for num in path:
        if not isinstance(node, dict) or num not in node:
            return False
        node = node[num]
    return True


def _when_value(enc, ln):
    """0.20 ruling 3: a `when` with a list or a range writes the varint of a
    field line with a present key at the same path when it matches the
    `when`, otherwise its first value. A `default` or a sentinel does not count."""
    if not getattr(ln, "listed", False):
        return ln.value
    values = enc.values
    for i, f in enc.plan.ifields:
        if i in values and i not in enc.sentinels and i not in enc.defaulted and f.loc == ln.loc:
            wt, payload = values[i]
            return payload if wt == VARINT and ln.holds_int(payload) else ln.value
    return ln.value


def _tree(pack, enc, kind_lines=()):
    """The field-number-keyed tree of one encoded scope. Where two written
    fields share a path the earliest line's value stands. A group writes one
    occurrence per item: a submessage, or with a `.` field the scalar; a `.`
    field with a numeric transform writes the packed form. An empty list
    writes nothing; an item with nothing written is an empty length-delimited
    occurrence. An integer `when` or `const` is written as a varint over any
    field; a `present` `when` of the selected kind (`kind_lines`) writes an
    empty length-delimited field when nothing under its path is written
    (0.5 ruling 14)."""
    tree = {}
    plan = enc.plan
    values = enc.values
    sentinels = enc.sentinels
    for i, ln in plan.ifields:
        if i in values and ln.loc and i not in sentinels:
            _set(tree, ln.loc, values[i], overwrite=False)
    for i, ln in plan.ifields:  # a sentinel where no field with a value writes (ruling 16 of 0.9)
        if i in sentinels and ln.loc:
            _set(tree, ln.loc, values[i], overwrite=False)
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if not children:
            continue
        scalar = _scalar_field(g)
        if scalar is None:
            node = _Rep((LEN, bytes(_serialize(_tree(pack, child)))) for child in children)
        else:
            leaves = [child.values.get(scalar[0]) for child in children]
            if scalar[1].transform.wire_type != LEN:
                # In a packed field an item with no value is left out, and a
                # list whose items all lack the value writes nothing (ruling 11 of 0.7).
                leaves = [leaf for leaf in leaves if leaf is not None]
                if not leaves:
                    continue
                node = (LEN, b"".join(_encode_varint(leaf[1]) if leaf[0] == VARINT else leaf[1] for leaf in leaves))
            else:
                node = _Rep(leaf if leaf is not None else (LEN, b"") for leaf in leaves)
        _set(tree, g.loc, node, overwrite=False)
    for ln in plan.lines:
        if not ln.is_field and not ln.is_group and not ln.present:
            _set(tree, ln.loc, (VARINT, _when_value(enc, ln)), overwrite=True)
    for ln in kind_lines:
        if not ln.is_field and not ln.is_group and ln.present and not _has(tree, ln.loc):
            _set(tree, ln.loc, (LEN, b""), overwrite=True)
    return tree


def write(pack, enc, kind_lines=()):
    """`enc` is the encoded top scope: `values` maps a field line's index to
    its `(wire_type, payload)`, `groups` a group line's index to the encoded
    scope of each item. Builds the canonical message of the mapped fields:
    ascending field-number order, each submessage serialised the same way."""
    return _serialize(_tree(pack, enc, kind_lines)).hex()
