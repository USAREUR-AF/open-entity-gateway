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

Armor (Version 0.14): with `armor sixbit` the input and output are armored
text, each character six bits, and the message is any number of bits. The
fixed-layout input must reach every bit the selected scope needs, whatever
`word_bytes` is; the output is the first `L` bits (the scope's furthest bit,
an `optional` field counted only when it is written), zero-filled to a
multiple of 6. A `sixbit` field's raw value is its six-bit values, first
character first, each read as a six-bit field in the pack's bit order.

Gate blocks (Version 0.17): `if [!]<locator> <value>[,...]` ... `end`. A line of
a block carries the innermost block as `gate`; each scope lists its blocks,
outer before nested, as `plan.gates`. Decode reads a line of a block only when
the block holds; encode chooses the held blocks in `_choose` (steps 1 to 4),
writes them in `_emit` (step 5) and checks the output in `write` (step 6).
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


def _armor_values():
    table = {}
    for c in range(0x30, 0x58):
        table[chr(c)] = c - 0x30
    for c in range(0x60, 0x78):
        table[chr(c)] = c - 0x38
    return table


_ARMOR = _armor_values()
_ARMOR_CHAR = "".join(chr(0x30 + v) if v < 40 else chr(0x38 + v) for v in range(64))


def armored_message(pack, data):
    """Armored text -> Message (0.14): every character is checked before any
    line is read; any character outside `0`-`W` and `` ` ``-`w`, white space
    and line terminators included, is `malformed-wire`. The message is
    `6 * <characters>` bits; the empty text is a message of no bits."""
    table = _ARMOR
    try:
        bits = "".join(format(table[c], "06b") for c in data)
    except KeyError:
        for c in data:
            if c not in table:
                raise MalformedWire("armored input: character %r is not a six-bit character" % c, format=pack.name)
        raise
    return Message.from_bits(pack, int(bits, 2) if bits else 0, len(bits))


def armored_text(acc, nbits):
    """The first `nbits` bits of `acc` (msb first, `acc` holding exactly
    `nbits` bits), followed by zero bits up to a multiple of 6, as armored
    text (0.14)."""
    fill = (-nbits) % 6
    acc <<= fill
    n = (nbits + fill) // 6
    return "".join(_ARMOR_CHAR[(acc >> (6 * (n - 1 - k))) & 63] for k in range(n))


def _start(pack, loc):
    return loc[0] * pack.word_bytes * 8 + loc[1]


def _ends(pack, plan):
    """-> (required, all): the furthest bit, counted from the scope's first
    bit, that the scope's own lines outside any gate block reach. `required`
    leaves out the fields with the `optional` option (decode); `all` counts
    every line (encode). A count locator and the start of a group count in
    both. The lines of a gate block count only when it holds (0.17)."""
    ends = plan.cache.get("ends")
    if ends is None:
        req = full = 0
        for ln in plan.lines:
            if ln.gate is not None:
                continue
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

    @classmethod
    def from_bits(cls, pack, value, nbits):
        """A message of `nbits` bits, `value` holding them in the pack's bit
        order (armored text, 0.14: any number of bits)."""
        msg = cls(pack, b"")
        msg.value = value
        msg.nbits = nbits
        return msg

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
    if pack.armor == "sixbit":
        msg = armored_message(pack, data)
        msg.nbytes = None
        return msg
    buf = _hex_to_bytes(data, pack.name)
    msg = Message(pack, buf)
    msg.nbytes = len(buf)
    return msg


def when_holds(pack, msg, when):
    """A `when` whose bits lie beyond the input does not hold."""
    start = _start(pack, when.loc)
    if start + when.loc[2] > msg.nbits:
        return False
    return when.holds_int(msg.uint(start, when.loc[2]))


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
    if plan.gates:
        # 0.17: a held block's fields that are not optional and its `const`
        # lines count; a gate locator does not (it is read like a `when`). A
        # block inside an item lies within the item, which is counted whole.
        held = gates_held(pack, msg, plan)
        for ln in plan.lines:
            if ln.gate is not None and held[ln.gate] and not (ln.is_field and ln.optional):
                end = max(end, _start(pack, ln.loc) + ln.loc[2])
    if pack.armor == "sixbit":  # every needed bit, whatever word_bytes is (0.14)
        if msg.nbits < end:
            raise MalformedWire(
                "message is %d bit(s); the format needs %d" % (msg.nbits, end), format=pack.name
            )
        return
    word_bits = pack.word_bytes * 8
    need = -(-end // word_bits) * pack.word_bytes
    if msg.nbytes < need:
        raise MalformedWire(
            "message is %d byte(s); the format needs %d" % (msg.nbytes, need), format=pack.name
        )


def gates_held(pack, view, plan):
    """-> {gate: whether its block holds} for the gate blocks of the scope read
    through `view` (0.17). A gate's bits are read as unsigned; a gate any of
    whose bits lies beyond the input does not hold, nor does its `!` form; a
    nested gate holds only when the block enclosing it holds."""
    msg = view.msg
    base = view.base
    held = {}
    for g in plan.gates:
        p = g.parent
        if p is not None and not held[p]:
            held[g] = False
            continue
        start = base + _start(pack, g.loc)
        length = g.loc[2]
        if start + length > msg.nbits:
            held[g] = False
            continue
        held[g] = (msg.uint(start, length) in g.vset) != g.negate
    return held


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
    if getattr(field.transform, "as_sixbit", False):
        return sixbit_values(msg, start, length)
    raw = msg.uint(start, length)
    return _swap_int(raw, length) if field.swap else raw


def is_absent(pack, field, raw):
    """Whether the raw value of a field with `absent` is one of its sentinels
    (spec 0.9): the field's bits as an unsigned integer, after `swap`. An
    `ascii` field's bytes, or a `sixbit` field's six-bit values (ruling 12 of
    0.14), are read back into that integer."""
    return raw_int(raw, pack.bit_order == "msb") in field.absent


def raw_int(raw, msb):
    """The field's bits as an unsigned integer, from a raw value that is an
    integer, `ascii` bytes, or `sixbit` values (first character first: the
    high six bits with `msb`, the low six bits with `lsb`)."""
    if isinstance(raw, (bytes, bytearray)):
        return int.from_bytes(raw, "big" if msb else "little")
    if isinstance(raw, (list, tuple)):
        n = 0
        seq = raw if msb else raw[::-1]
        for c in seq:
            n = (n << 6) | c
        return n
    return raw


def sixbit_values(msg, start, length):
    """The six-bit values of a `sixbit` field, first character first (0.14)."""
    return [msg.uint(start + 6 * k, 6) for k in range(length // 6)]


def count(pack, view, group):
    """The number of items of `group` in the scope of `view`: the fixed count,
    or the count read from the wire, before any item is laid out (ruling 7d of
    0.11)."""
    if group.count_loc is None:
        return group.count_n
    msg = view.msg
    cs = view.base + _start(pack, group.count_loc)
    if cs + group.count_loc[2] > msg.nbits:
        raise _short(pack, "the count of group %s" % group.list)
    return msg.uint(cs, group.count_loc[2])


def items(pack, view, group):
    """-> a view per item of `group`. On `bits` a list is always present, with
    `count` items."""
    msg = view.msg
    return [View(msg, b) for b in _item_bases(pack, msg, group, view.base)[0]]


# How a write stands against the others on the same bits (0.17 ruling 7): a
# `const`, `when`, count or field with a present key is STRONG; a `default` or
# a sentinel is WEAK; a gate value (GATE) is written only over bits no STRONG
# write covered. Between field lines ruling 5 of 0.5 holds, defaults included.
WEAK, STRONG, GATE = 0, 1, 2


class _Buf:
    """Bits being written, held as one integer of `nbits` bits in the pack's
    bit order. `strong` holds the bits any STRONG write covered, whatever wrote
    last: a gate value is written only over bits that only a `default` or a
    sentinel wrote (ruling 7 of 0.17). `written` holds the bits a STRONG or
    GATE write covered (step 3, ruling 11)."""

    __slots__ = ("nbits", "msb", "acc", "strong", "written")

    def __init__(self, nbits, msb):
        self.nbits = nbits
        self.msb = msb
        self.acc = 0
        self.strong = 0
        self.written = 0

    def _mask(self, start, length):
        shift = self.nbits - start - length if self.msb else start
        return shift, ((1 << length) - 1) << shift

    def write(self, start, length, v, flag):
        shift, m = self._mask(start, length)
        full = m
        if flag == GATE:
            m &= ~self.strong
        self.acc = (self.acc & ~m) | ((v << shift) & m)
        if flag == STRONG:
            self.strong |= m
            self.written |= m
        elif flag == GATE:
            self.written |= full

    def any_written(self, start, length):
        return bool(self.written & self._mask(start, length)[1])

    def read(self, start, length):
        shift, m = self._mask(start, length)
        return (self.acc & m) >> shift


def _field_ops(pack, enc, base, ops, keep):
    """Add the writes of the scope's `field` lines to `ops`, in the order they
    are applied (a later write stands over an earlier one). Where two written
    fields cover the same bits the earlier line's value stands; a field with a
    value, or a default, stands over a sentinel whatever their order (ruling
    16 of 0.9). With `keep` ({gate: bool}), a line of a gate block is written
    only when `keep` holds it; a held line whose value could not be encoded is
    `transform-encode`."""
    plan = enc.plan
    lines = plan.lines
    values = enc.values
    sentinels = enc.sentinels
    defaulted = enc.defaulted
    errors = enc.errors
    order = range(len(lines) - 1, -1, -1)  # later lines first, so earlier ones overwrite them
    if sentinels:
        order = [i for i in order if i in sentinels] + [i for i in order if i not in sentinels]
    for i in order:
        if keep is not None:
            g = lines[i].gate
            if g is not None:
                if not keep[g]:
                    continue
                if i in errors:
                    raise errors[i]
        if i not in values:
            continue
        ln = lines[i]
        flag = WEAK if (i in sentinels or i in defaulted) else STRONG
        start = base + _start(pack, ln.loc)
        v = values[i]
        if isinstance(v, (bytes, bytearray)):
            if ln.swap:
                v = v[::-1]
            for k, b in enumerate(v):
                ops.append((start + 8 * k, 8, b, flag))
        elif isinstance(v, tuple):  # the six-bit values of a `sixbit` field
            for k, c in enumerate(v):
                ops.append((start + 6 * k, 6, c, flag))
        else:
            v = int(v) & ((1 << ln.loc[2]) - 1)
            ops.append((start, ln.loc[2], _swap_int(v, ln.loc[2]) if ln.swap else v, flag))


def _when_value(pack, enc, ln, keep):
    """0.20 ruling 3: the value a `when` with a list or a range writes. When a
    field line with a present key covers exactly the `when`'s bits (the same
    offset and length) and the bits it writes match the `when`, those bits;
    otherwise the first value. A `default` or a sentinel does not count."""
    if not getattr(ln, "listed", False):
        return ln.value
    start = _start(pack, ln.loc)
    length = ln.loc[2]
    values = enc.values
    for i, f in enc.plan.ifields:
        if i not in values or i in enc.sentinels or i in enc.defaulted:
            continue
        if f.loc[2] != length or _start(pack, f.loc) != start:
            continue
        if keep is not None and f.gate is not None and not keep[f.gate]:
            continue
        v = values[i]
        if isinstance(v, (bytes, bytearray)):
            return ln.value
        v = int(v) & ((1 << length) - 1)
        if f.swap:
            v = _swap_int(v, length)
        return v if ln.holds_int(v) else ln.value  # the earliest line's value stands (0.5 ruling 5)
    return ln.value


def _fixed_ops(pack, plan, base, ops, keep, enc=None):
    """Add the writes of the scope's `when` and `const` lines (0.5 ruling 5:
    over any field). With `keep`, a `const` of a gate block only when held."""
    for ln in plan.lines:
        if not ln.is_field and not ln.is_group:
            if keep is not None and ln.gate is not None and not keep[ln.gate]:
                continue
            value = _when_value(pack, enc, ln, keep) if enc is not None else ln.value
            ops.append((base + _start(pack, ln.loc), ln.loc[2], value, STRONG))


def _choose(pack, enc):
    """Steps 1 to 4 of 0.17's encode for one scope, in the scope's own bits ->
    ({gate: held}, {gate: its gate value}). A block is open when a field line
    of it, or of a nested open block, maps a key the scope holds. The lines
    outside any block are written; each open block none of whose gate bits is
    written then gets its gate value, in line order; each block is evaluated on
    those bits, bits nothing wrote reading as 0. The bits an item's lines
    write are not part of the scope (ruling 9)."""
    plan = enc.plan
    gates = plan.gates
    have = enc.have or {}
    opened = dict.fromkeys(gates, False)
    suppressed = enc.suppressed
    for i, f in plan.ifields:
        g = f.gate
        if g is not None and f.key in have and i not in suppressed:  # 0.18 ruling 1
            while g is not None and not opened[g]:
                opened[g] = True
                g = g.parent
    nbits = 0
    for ln in plan.lines:
        if ln.is_group:
            if ln.count_loc is not None:
                nbits = max(nbits, _start(pack, ln.count_loc) + ln.count_loc[2])
        else:
            nbits = max(nbits, _start(pack, ln.loc) + ln.loc[2])
    for g in gates:
        nbits = max(nbits, _start(pack, g.loc) + g.loc[2])
    buf = _Buf(nbits, pack.bit_order == "msb")
    ops = []
    outside = dict.fromkeys(gates, False)
    _field_ops(pack, enc, 0, ops, outside)
    _fixed_ops(pack, plan, 0, ops, outside, enc)
    for i, g in plan.igroups:
        if g.count_loc is not None:
            n = len(enc.groups.get(i) or ())
            ops.append((_start(pack, g.count_loc), g.count_loc[2], n & ((1 << g.count_loc[2]) - 1), STRONG))
    for op in ops:
        buf.write(*op)
    values = {}
    for g in gates:
        if not opened[g] or g.enc_value is None:
            continue
        start = _start(pack, g.loc)
        if buf.any_written(start, g.loc[2]):
            continue  # the block is evaluated on the bits as they are
        buf.write(start, g.loc[2], g.enc_value, GATE)
        values[g] = g.enc_value
    held = {}
    for g in gates:
        p = g.parent
        held[g] = (p is None or held[p]) and (buf.read(_start(pack, g.loc), g.loc[2]) in g.vset) != g.negate
    return held, values


def _emit(pack, enc, base, ops, checks):
    """Add to `ops` the `(start, length, value, flag)` writes of one scope that
    starts at bit `base`, in the order they are applied; -> the bit after the
    furthest bit the scope reaches. `when` and `const` values are written over
    any field (0.5 ruling 5), and a count over any field on its bits (ruling 2
    of 0.7). With gate blocks (0.17) only the lines outside any block and the
    lines of the held blocks are written, with the gate value of each held
    block that got one; the scope is added to `checks` for step 6."""
    plan = enc.plan
    held = None
    if plan.gates:
        held, gate_values = _choose(pack, enc)
    armored = pack.armor == "sixbit"
    if armored:
        # `L` of 0.14: an `optional` field counts only when it is written (its
        # key present, a `default`, or its first sentinel: ruling 12 of 0.14),
        # and a line of a gate block only when the block is held (0.17)
        end = base + _ends(pack, plan)[0]
        lines = plan.lines
        for i in enc.values:
            ln = lines[i]
            if ln.optional and (ln.gate is None or held[ln.gate]):
                end = max(end, base + _start(pack, ln.loc) + ln.loc[2])
    else:
        end = base + _ends(pack, plan)[1]
    _field_ops(pack, enc, base, ops, held)
    if held is not None:
        for g in plan.gates:
            if held[g]:
                end = max(end, base + _start(pack, g.loc) + g.loc[2])
                if g in gate_values:
                    ops.append((base + _start(pack, g.loc), g.loc[2], gate_values[g], GATE))
        for i, ln in enumerate(plan.lines):
            if ln.gate is not None and held[ln.gate]:
                if armored and ln.is_field and ln.optional:
                    continue  # counted above when written
                end = max(end, base + _start(pack, ln.loc) + ln.loc[2])
        have = enc.have or {}
        unheld = set()
        kept = set()
        for i, f in plan.ifields:
            if f.key in have and i not in enc.suppressed:  # 0.18 ruling 1: not mapped for step 6
                if f.gate is None or held[f.gate]:
                    kept.add(f.key)
                else:
                    unheld.add(f.key)
        lost = unheld - kept
        if lost:
            key = min(lost, key=lambda k: next(i for i, f in plan.ifields if f.key == k))
            loc = next(f.locator for f in plan.fields if f.key == key)
            raise TransformEncodeFailure(
                "key %s is mapped only by lines of gate blocks that do not hold" % key,
                format=pack.name, locator=loc,
            )
        checks.append((plan, base, held))
    _fixed_ops(pack, plan, base, ops, held, enc)
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
            counts.append((base + _start(pack, g.count_loc), g.count_loc[2], n, STRONG))
        pos = base + _start(pack, g.loc)
        for child in children or ():
            pos = max(pos + g.item_bits, _emit(pack, child, pos, ops, checks))
        end = max(end, pos)
    ops.extend(counts)
    return end


def write(pack, enc):
    """`enc` is the encoded top scope: `values` maps a field line's index to
    its raw value, `groups` a group line's index to the encoded scope of each
    item. Bits no line covers are zero; the message is whole words. Returns
    lowercase hex. With gate blocks, the output must decode to the blocks the
    encode chose (step 6 of 0.17), checked per scope on the whole output."""
    ops = []
    checks = []
    end = _emit(pack, enc, 0, ops, checks)
    armored = pack.armor == "sixbit"
    if armored:  # `L` bits of 0.14, in msb order (`armor sixbit` needs `bit_order msb`)
        nbits = end
        msb = True
    else:
        word_bits = pack.word_bytes * 8
        nbytes = -(-end // word_bits) * pack.word_bytes
        nbits = nbytes * 8
        msb = pack.bit_order == "msb"
    acc = _lay_out(ops, nbits, msb, bool(checks))
    if armored:
        msg = Message.from_bits(pack, acc, nbits) if checks else None
    else:
        out = acc.to_bytes(nbytes, "big" if msb else "little")
        msg = Message(pack, out) if checks else None
    for plan, base, held in checks:
        if gates_held(pack, View(msg, base), plan) != held:
            raise TransformEncodeFailure(
                "the output does not decode to the gate blocks the encode chose", format=pack.name,
            )
    return armored_text(acc, end) if armored else out.hex()


def _lay_out(ops, nbits, msb, gated=False):
    """The `nbits` bits the writes give, as one integer in bit order; a later
    write stands over an earlier one, except that a gate value is written only
    over bits that only a `default` or a sentinel wrote (`gated`, ruling 7 of
    0.17)."""
    if gated:
        buf = _Buf(nbits, msb)
        for op in ops:
            buf.write(*op)
        return buf.acc
    acc = 0
    for start, length, v, _ in ops:
        shift = nbits - start - length if msb else start
        mask = (1 << length) - 1
        acc = (acc & ~(mask << shift)) | ((v & mask) << shift)
    return acc
