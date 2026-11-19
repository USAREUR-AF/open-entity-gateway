"""The `bits` wire: binary words whose fields are bit ranges.

Input and output are hex text (spec/engine-spec.md, "Version 0.2: the bits
wire"). `<word>:<pos>:<len>` addresses `len` bits starting at bit
`word * word_bytes * 8 + pos` of the message; a field may run past its word
(Version 0.5). The message is as many whole words as the furthest mapped or
`when` bit reaches, over the common lines and the selected kind.

Bit order (`bit_order`): with `lsb`, bit `i` of the message is bit `i mod 8` of
byte `i div 8` and a field's first bit is its least significant; with `msb`, bit
`i` is bit `7 - (i mod 8)` of byte `i div 8` and a field's first bit is its most
significant.

A field's raw value is its bits as an unsigned integer, which the field's
transform interprets; an `ascii` field's raw value is its bytes, first to last,
each byte read as an eight-bit field in the pack's bit order.

Groups (Version 0.7): `group <list> <word>:<pos> item <bits> count <locator | n>`.
The items lie back to back from the group's start; inside an item a locator is
counted from the item's first bit. An item is `item <bits>` long, or longer when
a nested group counted by a locator runs past that; the next item starts where
it ends (ruling 4 of 0.7). A scope is read through a `View`: the message and
the bit the scope starts at.
"""

import re

from .errors import MalformedWire, TransformEncodeFailure


_HEX_WS = {ord(c): None for c in " \t\r\n"}
_NOT_HEX = re.compile("[^0-9a-fA-F]")


def _hex_to_bytes(data, fmt):
    """Hex text -> bytes (ruling 36 of 0.8). Space, tab, carriage return and
    line feed are ignored wherever they stand; any other character that is not
    one of `0-9a-fA-F` (a sign, a digit of another script, any other white
    space) and an odd number of digits are `malformed-wire`."""
    cleaned = data.translate(_HEX_WS)
    bad = _NOT_HEX.search(cleaned)
    if bad is not None:
        raise MalformedWire("input is not hexadecimal: character %r" % bad.group(), format=fmt)
    if len(cleaned) % 2 != 0:
        raise MalformedWire("hex input has an odd number of digits", format=fmt)
    return bytes.fromhex(cleaned)


def _start(pack, loc):
    return loc[0] * pack.word_bytes * 8 + loc[1]


def _ends(pack, plan):
    """-> (required, all): the furthest bit, counted from the scope's first
    bit, that the scope's own lines reach. `required` leaves out the fields
    with the `optional` option (decode); `all` counts every line (encode). A
    count locator and the start of a group count in both."""
    ends = plan.cache.get("ends")
    if ends is None:
        req = full = 0
        for ln in plan.lines:
            if ln.is_group:
                end = _start(pack, ln.loc)
                if ln.count_loc is not None:
                    end = max(end, _start(pack, ln.count_loc) + ln.count_loc[2])
            else:
                end = _start(pack, ln.loc) + ln.loc[2]
            full = max(full, end)
            if not (ln.is_field and ln.optional):
                req = max(req, end)
        ends = plan.cache["ends"] = (req, full)
    return ends


def _item_size(pack, group):
    """The size in bits every item of `group` has, or None when it varies: when
    a group counted by a locator is nested in the item, at any depth."""
    cache = group.plan.cache
    if "size" not in cache:
        size = group.item_bits
        for g in group.plan.groups:
            if g.count_loc is not None or _item_size(pack, g) is None:
                size = None
                break
        cache["size"] = size
    return cache["size"]


def message_bytes(pack, lines):
    """Whole words up to the furthest bit any of `lines` reaches, in bytes."""
    end = 0
    for ln in lines:
        if not ln.is_group:
            end = max(end, _start(pack, ln.loc) + ln.loc[2])
    word_bits = pack.word_bytes * 8
    return -(-end // word_bits) * pack.word_bytes


class Message:
    """The input bytes, held as one integer in the pack's bit order. It is also
    the view of the top scope: `msg` is itself and `base` is 0."""

    base = 0

    def __init__(self, pack, buf):
        self.nbits = len(buf) * 8
        self.msb = pack.bit_order == "msb"
        self.value = int.from_bytes(buf, "big" if self.msb else "little")
        self.msg = self

    def uint(self, start, length):
        """The `length` bits from bit `start` as an unsigned integer. The caller
        has checked that they lie within the input."""
        if self.msb:
            return (self.value >> (self.nbits - start - length)) & ((1 << length) - 1)
        return (self.value >> start) & ((1 << length) - 1)


class View:
    """One item of a group: the message and the item's first bit."""

    __slots__ = ("msg", "base")

    def __init__(self, msg, base):
        self.msg = msg
        self.base = base


def parse(pack, data):
    buf = _hex_to_bytes(data, pack.name)
    msg = Message(pack, buf)
    msg.nbytes = len(buf)
    return msg


def when_holds(pack, msg, when):
    """A `when` whose bits lie beyond the input does not hold."""
    start = _start(pack, when.loc)
    if start + when.loc[2] > msg.nbits:
        return False
    return msg.uint(start, when.loc[2]) == when.value


def _swap_int(value, length):
    """The `swap` option on an integer raw value: its `length / 8` bytes in reverse order."""
    n = length // 8
    return int.from_bytes(value.to_bytes(n, "big"), "little")


def _short(pack, what):
    return MalformedWire("the input ends before %s" % what, format=pack.name)


def _item_bases(pack, msg, group, base):
    """-> (the first bit of each item of `group`, the bit after the last), for
    the scope that starts at bit `base`. The count is read from the count
    locator of that scope, or is the fixed count. Input that does not hold the
    count or every item whole is `malformed-wire`."""
    if group.count_loc is None:
        n = group.count_n
    else:
        cs = base + _start(pack, group.count_loc)
        if cs + group.count_loc[2] > msg.nbits:
            raise _short(pack, "the count of group %s" % group.list)
        n = msg.uint(cs, group.count_loc[2])
    start = base + _start(pack, group.loc)
    size = _item_size(pack, group)
    if size is not None:
        end = start + n * size
        if n and end > msg.nbits:
            raise _short(pack, "the %d item(s) of group %s" % (n, group.list))
        return range(start, end, size), end
    bases = []
    pos = start
    plan = group.plan
    for _ in range(n):
        if pos + group.item_bits > msg.nbits:
            raise _short(pack, "the %d item(s) of group %s" % (n, group.list))
        bases.append(pos)
        pos = max(pos + group.item_bits, _measure(pack, msg, plan, pos))
    return bases, pos


def _measure(pack, msg, plan, base):
    """The bit after the furthest bit the scope at `base` needs: its lines that
    are not optional, and every item of its groups."""
    end = base + _ends(pack, plan)[0]
    for g in plan.groups:
        end = max(end, _item_bases(pack, msg, g, base)[1])
    return end


def check_length(pack, msg, plan):
    """Input that does not reach every mapped bit is `malformed-wire`: whole
    words up to the furthest bit that is not optional (0.6 ruling 14), the
    items of the groups counted in (0.7). A field with the `optional` option
    is not counted: it is absent when the input does not reach it (0.6)."""
    if plan.groups:
        end = _measure(pack, msg, plan, 0)
    else:
        end = _ends(pack, plan)[0]
    word_bits = pack.word_bytes * 8
    need = -(-end // word_bits) * pack.word_bytes
    if msg.nbytes < need:
        raise MalformedWire(
            "message is %d byte(s); the format needs %d" % (msg.nbytes, need), format=pack.name
        )


def get(pack, view, field):
    """The raw value of one field: an unsigned integer, or bytes for `ascii`;
    None when the field is `optional` and any of its bits lies beyond the
    input. With `swap` the field's bytes are taken in reverse order."""
    msg = view.msg
    start = view.base + _start(pack, field.loc)
    length = field.loc[2]
    if start + length > msg.nbits:
        if field.optional:
            return None
        raise _short(pack, "the field at %s" % field.locator)
    if getattr(field.transform, "as_bytes", False):
        raw = bytes(msg.uint(start + 8 * k, 8) for k in range(length // 8))
        return raw[::-1] if field.swap else raw
    raw = msg.uint(start, length)
    return _swap_int(raw, length) if field.swap else raw


def items(pack, view, group):
    """-> a view per item of `group`. On `bits` a list is always present, with
    `count` items."""
    msg = view.msg
    return [View(msg, b) for b in _item_bases(pack, msg, group, view.base)[0]]


def _emit(pack, enc, base, ops):
    """Add to `ops` the `(start, length, value)` writes of one scope that
    starts at bit `base`, in the order they are applied (a later write stands
    over an earlier one); -> the bit after the furthest bit the scope reaches.
    Where two written fields cover the same bits the earlier line's value
    stands; `when` and `const` values are written over any field (0.5 ruling
    5), and a count over any field on its bits (ruling 2 of 0.7)."""
    plan = enc.plan
    lines = plan.lines
    values = enc.values
    end = base + _ends(pack, plan)[1]
    for i in range(len(lines) - 1, -1, -1):  # later lines first, so earlier ones overwrite them
        if i not in values:
            continue
        ln = lines[i]
        start = base + _start(pack, ln.loc)
        v = values[i]
        if isinstance(v, (bytes, bytearray)):
            if ln.swap:
                v = v[::-1]
            for k, b in enumerate(v):
                ops.append((start + 8 * k, 8, b))
        else:
            v = int(v) & ((1 << ln.loc[2]) - 1)
            ops.append((start, ln.loc[2], _swap_int(v, ln.loc[2]) if ln.swap else v))
    for ln in lines:
        if not ln.is_field and not ln.is_group:
            ops.append((base + _start(pack, ln.loc), ln.loc[2], ln.value))
    counts = []
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if g.count_loc is None:
            if children is None:  # ruling 5 of 0.7
                raise TransformEncodeFailure(
                    "the entity has no list %s; the group has the fixed count %d" % (g.list, g.count_n),
                    format=pack.name, locator=g.locator,
                )
            if len(children) != g.count_n:
                raise TransformEncodeFailure(
                    "list %s has %d item(s); the group has the fixed count %d" % (g.list, len(children), g.count_n),
                    format=pack.name, locator=g.locator,
                )
        else:
            n = len(children) if children is not None else 0
            if n >= (1 << g.count_loc[2]):
                raise TransformEncodeFailure(
                    "list %s has %d item(s), which does not fit the %d-bit count" % (g.list, n, g.count_loc[2]),
                    format=pack.name, locator=g.locator,
                )
            counts.append((base + _start(pack, g.count_loc), g.count_loc[2], n))
        pos = base + _start(pack, g.loc)
        for child in children or ():
            pos = max(pos + g.item_bits, _emit(pack, child, pos, ops))
        end = max(end, pos)
    ops.extend(counts)
    return end


def write(pack, enc):
    """`enc` is the encoded top scope: `values` maps a field line's index to
    its raw value, `groups` a group line's index to the encoded scope of each
    item. Bits no line covers are zero; the message is whole words. Returns
    lowercase hex."""
    ops = []
    end = _emit(pack, enc, 0, ops)
    word_bits = pack.word_bytes * 8
    nbytes = -(-end // word_bits) * pack.word_bytes
    nbits = nbytes * 8
    msb = pack.bit_order == "msb"
    acc = 0
    for start, length, v in ops:
        shift = nbits - start - length if msb else start
        mask = (1 << length) - 1
        acc = (acc & ~(mask << shift)) | ((v & mask) << shift)
    return acc.to_bytes(nbytes, "big" if msb else "little").hex()
