"""The `bits` wire with `layout sequential` (spec/engine-spec.md, "Version
0.8: sequential binary layouts"): the lines of the pack are read, and
written, in file order from a cursor.

Decode keeps two kinds of end. The end of the input, or the length a
`length ... whole` line states, is hard: reading past it is `malformed-wire`.
The end of a `length` block is soft: a line that starts at or after it reads
as absent, a line that straddles it is `malformed-wire` (ruling 7 of 0.8). An
indicator bit and a presence field there read as 0, the continuation octet of
an `fx` presence there is `malformed-wire` (ruling 21), and `align` stops at
either end without error (ruling 20).

Encode computes a presence bit before it writes it: the bit is 1 when some
`if` block naming it without `!` has content (a present key, a key with a
default, a non-empty list), wherever in the presence's block that `if` stands,
group items included. A length is written after its block.

Under `armor sixbit` (0.14) the input and output are armored text: the
message is any number of bits, and the output is the bits up to the cursor
after the last line, not rounded to a byte, zero-filled to a multiple of 6.
"""

from .entity import Entity, Item, _via_decode, _via_encode
from .errors import GTError, MalformedWire, NoKind, TransformDecodeFailure, TransformEncodeFailure
from .seqdef import (
    T_ALIGN, T_CHAIN, T_CHOICE, T_CONST, T_COUNT, T_EXT, T_FIELD, T_GROUP, T_IF, T_LENGTH, T_OPT, T_PAD,
    T_PRESENCE, T_SWITCH, T_WHEN, T_WHOLE, children,
)
from .transforms import value_canon
from .uper import INT64_MAX, twos_octets, uint_octets
from .wire_bits import reverse_bits, Message, _hex_to_bytes, _swap_int, armored_message, armored_text, raw_int, sixbit_values

_NONE = frozenset()


class _UperValue:
    """What a field line with a UPER locator writes (0.19): the transform's
    value `v`, for the key of `f`; `_Writer.field` writes it."""

    __slots__ = ("v", "f")

    def __init__(self, v, f):
        self.v = v
        self.f = f


class _UntilCodes:
    """What a field line with the locator `until:<code>` writes (0.22): the
    codes of its characters; `_Writer.field` adds the terminator."""

    __slots__ = ("codes",)

    def __init__(self, codes):
        self.codes = codes


_UPER_ABSENT = object()  # a UPER locator whose key is absent: the value of ruling 1 of 0.19


def _uper_absent_raw(loc):
    """What decode reads from the bits `_Writer.uper_absent` writes (ruling 1
    of 0.19): `lb`, or 0, for an integer; index 0; `lb` characters of code 0;
    `lb` zero octets."""
    if loc.form == "int":
        return loc.lb if loc.lb is not None else 0
    if loc.form == "enum":
        return 0
    if loc.form == "chars":
        return [0] * (loc.lb or 0)
    return bytes(loc.lb or 0)
_MISSING = object()
_FAILED = object()  # encode: the bits a line wrote for the key do not decode (0.20 ruling 31)


def select_case(nd, value):
    """0.20: the case of switch `nd` for the key's value (`_MISSING` when the
    key is absent): the first case whose list holds its canonical text, else
    the `else`; None when there is no `else` (`no-kind`)."""
    if value is _MISSING:
        return nd.other
    text = value_canon(value, nd.key_type)
    for case in nd.cases:
        if text in case.canon:
            return case
    return nd.other


def _no_case(pack, nd, sc_item):
    return NoKind("the %s matches no case of 'switch %s' and the switch has no 'else'"
                  % ("item" if sc_item else "message", nd.key), format=pack.name, locator=nd.key)


class _TooBig(Exception):
    """A UPER value the engine does not hold: `transform-decode` (rulings 5 and 19 of 0.19)."""


def _twos(raw, nbits):
    return raw - (1 << nbits) if nbits and raw >> (nbits - 1) else raw


# --- decode ----------------------------------------------------------------


class _Scope:
    """What one message or one item decodes into."""

    __slots__ = ("fields", "lists", "prefix", "failed")

    def __init__(self, fields, lists, prefix):
        self.fields = fields
        self.lists = lists
        self.prefix = prefix
        self.failed = None  # [(key, locator, reason, final)]


def _held(items):
    """The items a list holds, with the items of the lists inside them (ruling 7a of 0.11)."""
    n = len(items)
    for it in items:
        for inner in it.lists.values():
            n += _held(inner)
    return n


class _Reader:
    def __init__(self, pack, data, limit=None):
        self.left = limit  # the items the entity may still hold, None for no limit (ruling 7 of 0.11)
        self.pack = pack
        if pack.armor == "sixbit":
            self.msg = armored_message(pack, data)
        else:
            self.msg = Message(pack, _hex_to_bytes(data, pack.name))
        self.pos = 0
        self.hard = self.msg.nbits
        self.soft = []      # the ends of the open `length` blocks, outermost first
        # 0.21: the unit of each open `length` block ("bytes" | "bits"), None for a
        # block that starts at or past the end of its enclosing extent (not read)
        self.units = []
        self.counts = {}    # 0.21: count node -> the number of items it states, None when not read
        self.env = {}       # presence node -> the set of its bits that are 1
        self.failures = []  # (key, locator, reason)
        # 0.20 ruling 21: the bits that are 1 and that only `if` lines of cases
        # name, per reading of a presence; a selected case clears the bits it names
        self.pending = {}   # presence node -> [the bits of each of its readings still to be named]
        self.readings = []  # (presence node, bits still to be named) of every reading

    def short(self, what):
        return MalformedWire("the input, or a length extent, ends before %s" % what, format=self.pack.name)

    def limit(self):
        return self.soft[-1] if self.soft else self.hard

    def take(self, n, what):
        """-> the first bit of the next `n` bits, the cursor moved past them;
        None when the line starts at or after the end of a `length` extent."""
        pos = self.pos
        if self.soft:
            end = self.soft[-1]
            if pos >= end:
                return None
            if pos + n > end:
                raise self.short(what)
        if pos + n > self.hard:
            raise self.short(what)
        self.pos = pos + n
        return pos

    # -- lines --

    def run(self, nodes, sc):
        for nd in nodes:
            t = nd.t
            if t == T_FIELD:
                self.field(nd, sc)
            elif t == T_CONST or t == T_PAD:
                self.take(nd.length, "a '%s' of %d bit(s)" % ("const" if t == T_CONST else "pad", nd.length))
            elif t == T_ALIGN:
                # ruling 20 of 0.8: `align` stops at the end of the input or extent without error
                end = min(self.pos + (-self.pos) % nd.length, self.limit(), self.hard)
                if end > self.pos:
                    self.pos = end
            elif t == T_WHEN:  # a common `when`
                if not self.when_holds(nd):
                    raise NoKind("a common 'when' does not hold (%d bits, %d)" % (nd.length, nd.value),
                                 format=self.pack.name)
            elif t == T_PRESENCE:
                self.presence(nd)
            elif t == T_IF:
                env = self.env
                for p, bit, neg in nd.conds:
                    if (bit in env.get(p, _NONE)) == neg:
                        break
                else:
                    self.run(nd.body, sc)
            elif t == T_OPT:
                start = self.take(1, "the bit of an 'opt'")
                if start is not None and self.msg.uint(start, 1):
                    self.run(nd.body, sc)
            elif t == T_GROUP:
                self.group(nd, sc)
            elif t == T_COUNT:
                self.count(nd)
            elif t == T_CHAIN:
                self.chain(nd, sc)
            elif t == T_LENGTH:
                self.length(nd, sc)
            elif t == T_EXT:
                self.ext(nd, sc)
            elif t == T_CHOICE:
                self.choice(nd, sc)
            elif t == T_SWITCH:
                key = nd.key
                if key not in sc.fields and sc.failed:  # ruling 35 of 0.20
                    bad = [(sc.prefix + k, loc, why) for k, loc, why, _ in sc.failed if k == key]
                    if bad:
                        raise TransformDecodeFailure(
                            "the key of 'switch %s' could not be decoded" % key, format=self.pack.name, absent=bad
                        )
                case = select_case(nd, sc.fields.get(key, _MISSING))
                if case is None:
                    raise _no_case(self.pack, nd, sc.prefix)
                pending = self.pending
                for p, bit in case.if_names:  # ruling 36: every earlier reading of the presence
                    for rest in pending.get(p, ()):
                        rest.discard(bit)
                self.run(case.nodes, sc)
            else:
                self.whole(nd)

    def when_holds(self, nd):
        """A `when` whose bits lie beyond the input does not hold."""
        pos = self.pos
        if pos + nd.length > self.limit():
            return False
        self.pos = pos + nd.length
        v = self.msg.uint(pos, nd.length)
        for lo, hi in nd.ranges:
            if lo <= v <= hi:
                return True
        return False

    # -- UPER (0.19) --

    def at_soft_end(self):
        """True when the cursor is at or after the end of a `length` extent: a
        line that starts there reads as absent."""
        return bool(self.soft) and self.pos >= self.soft[-1]

    def need(self, n, what):
        """-> the next `n` bits as an unsigned number; reading past the input or
        the extent is `malformed-wire`."""
        if n == 0:
            return 0
        start = self.take(n, what)
        if start is None:
            raise self.short(what)
        return self.msg.uint(start, n)

    def constrained(self, lb, ub, what):
        rng = ub - lb
        raw = self.need(rng.bit_length(), what)
        if raw > rng:
            raise MalformedWire("%s: %d is outside its constraint %d..%d" % (what, lb + raw, lb, ub),
                                format=self.pack.name)
        return lb + raw

    def length_det(self, what):
        if not self.need(1, what):
            return self.need(7, what)
        if not self.need(1, what):
            return self.need(14, what)
        raise MalformedWire("%s: a fragmented length (first bits 11)" % what, format=self.pack.name)

    def int_octets(self, what):
        """-> (count, raw) of an integer read from octets. The checks run in the
        order of ruling 20 of 0.19: the input holds the octets, the count is
        not 0 (both `malformed-wire`); the caller checks that the value fits."""
        n = self.length_det(what)
        raw = self.need(8 * n, what)
        if n == 0:
            raise MalformedWire("%s: an integer of 0 octets" % what, format=self.pack.name)
        return n, raw

    def normally_small(self, what):
        if not self.need(1, what):
            return self.need(6, what)
        n, raw = self.int_octets(what)
        if n > 8:
            raise _TooBig("%s: a number of %d octets" % (what, n))
        return raw

    def uper_raw(self, loc, what):
        """-> what the transform of a UPER locator decodes: an integer, a list of
        7-bit codes, or bytes."""
        form = loc.form
        if form == "int":
            if loc.ub is not None and not (loc.ext and self.need(1, what)):
                return self.constrained(loc.lb, loc.ub, what)
            n, raw = self.int_octets(what)
            if n > 8:
                raise _TooBig("%s: an integer of %d octets" % (what, n))
            if loc.ub is not None or loc.lb is None:
                return _twos(raw, 8 * n)
            v = loc.lb + raw
            if v > INT64_MAX:
                raise _TooBig("%s: %d does not fit a signed 64-bit integer" % (what, v))
            return v
        if form == "enum":
            if loc.ext and self.need(1, what):
                v = loc.n + self.normally_small(what)
                if v > INT64_MAX:
                    raise _TooBig("%s: extension index %d does not fit a signed 64-bit integer" % (what, v))
                return v
            return self.constrained(0, loc.n - 1, what)
        count = self.constrained(loc.lb, loc.ub, what) if loc.ub is not None else self.length_det(what)
        if form == "chars":
            raw = self.need(7 * count, what)
            return [(raw >> (7 * (count - 1 - k))) & 0x7F for k in range(count)]
        return self.need(8 * count, what).to_bytes(count, "big")

    def skip_open_type(self, what):
        n = self.length_det(what)
        self.need_skip(8 * n, what)

    def need_skip(self, n, what):
        if n and self.take(n, what) is None:
            raise self.short(what)

    def ext(self, nd, sc):
        start = self.take(1, "the extension bit of an 'ext' block")
        if start is None:
            return
        self.run(nd.body, sc)
        if not self.msg.uint(start, 1):
            return
        what = "the extension additions of an 'ext' block"
        if not self.need(1, what):  # ruling 16 of 0.19
            m = self.need(6, what) + 1
        else:
            m = self.length_det(what)
        bits = self.need(m, what)
        for k in range(m):
            if (bits >> (m - 1 - k)) & 1:
                self.skip_open_type(what)

    def choice(self, nd, sc):
        what = "the index of a 'choice'"
        if nd.ext:
            start = self.take(1, "the extension bit of a 'choice'")
            if start is None:
                return
            if self.msg.uint(start, 1):
                try:
                    self.normally_small(what)
                except _TooBig as e:  # no key holds it: the decode fails (ruling 20 of 0.19)
                    raise TransformDecodeFailure(str(e), format=self.pack.name, locator="choice %d" % nd.n)
                self.skip_open_type("the open type of an extension alternative")
                return
        elif self.at_soft_end():
            return
        i = self.constrained(0, nd.n - 1, what)
        alt = nd.index.get(i)
        if alt is None:
            raise MalformedWire("the 'choice' index %d has no 'alt'" % i, format=self.pack.name)
        self.run(alt.body, sc)

    def field(self, nd, sc):
        if nd.uper is not None:
            if self.at_soft_end():
                return
            try:
                raw = self.uper_raw(nd.uper, "field %s (%s)" % (nd.bindings[0].key, nd.locator))
            except _TooBig as e:
                for f in nd.bindings:
                    if f.key not in sc.fields:
                        self.fail(sc, f.key, f.locator, str(e), False)
                return
            self.values(nd, sc, raw)
            return
        if nd.until is not None:
            self.until(nd, sc)
            return
        length = nd.length
        start = self.take(length, "a field of %d bit(s)" % length)
        if start is None:
            return
        msg = self.msg
        first = nd.bindings[0]
        as_bytes = getattr(first.transform, "as_bytes", False)
        if as_bytes:
            raw = bytes(msg.uint(start + 8 * k, 8) for k in range(length // 8))
            if first.swap:
                raw = raw[::-1]
        elif getattr(first.transform, "as_sixbit", False):
            raw = sixbit_values(msg, start, length)
        else:
            raw = msg.uint(start, length)
            if first.swap:
                raw = _swap_int(raw, length)
        if first.absent is not None:  # a sentinel (0.9): compared with the bits as an integer, after `swap`
            if raw_int(raw, msg.msb) in first.absent:  # `sixbit` too (ruling 12 of 0.14)
                return
        self.values(nd, sc, raw)

    def until(self, nd, sc):
        """`until:<code> chars:<set>` (0.22): characters up to the terminator,
        which is consumed. A field that starts at the end of a `length` extent
        is absent; reaching the end of the extent, or more than `2040 / w`
        characters, before the terminator is `malformed-wire` (rulings 4 and
        8). The terminator is found from the codes alone; the characters are
        decoded after it, so a bad one is `transform-decode` and the cursor
        stands after the terminator (ruling 5)."""
        if self.at_soft_end():
            return
        t = nd.until
        w = t.width
        msg = self.msg
        end = self.limit()
        codes = []
        what = "the terminator %d of field %s" % (t.code, nd.bindings[0].key)
        while True:
            if self.pos + w > end:
                raise self.short(what)
            c = msg.uint(self.pos, w)
            self.pos += w
            if c == t.code:
                break
            if len(codes) == t.most:
                raise MalformedWire("%s: more than %d character(s) before it" % (what, t.most),
                                    format=self.pack.name)
            codes.append(c)
        self.values(nd, sc, codes)

    def values(self, nd, sc, raw):
        """Decode the raw value of a field line into each of its keys."""
        n2c = self.pack.native_to_cot
        fields = sc.fields
        for f in nd.bindings:  # the line's key and each `also` key: one transform, the table of each
            key = f.key
            if key in fields:
                continue  # an earlier line of the key stands (0.6, "One key at several locators")
            try:
                value = f.transform.decode(raw)
            except ValueError as e:
                self.fail(sc, key, f.locator, str(e), False)
                continue
            if f.via_dec is not None:
                try:
                    value = _via_decode(f, value)
                except ValueError as e:
                    self.fail(sc, key, f.locator, str(e), True)
                    continue
            if n2c and key == "type" and value in n2c:
                value = n2c[value]
            fields[key] = value

    @staticmethod
    def fail(sc, key, locator, why, final):
        if sc.failed is None:
            sc.failed = []
        sc.failed.append((key, locator, why, final))

    def settle(self, sc):
        """A line whose transform failed is an error only when no line of its key succeeded."""
        if sc.failed:
            self.failures.extend(
                (sc.prefix + k, loc, why) for k, loc, why, final in sc.failed if final or k not in sc.fields
            )

    def presence(self, nd):
        msg = self.msg
        bits = set()
        if nd.length is not None:
            start = self.take(nd.length, "the %d presence bit(s) of %s" % (nd.length, nd.name))
            if start is not None:
                for k in range(nd.length):
                    if msg.uint(start + k, 1):
                        bits.add(k + 1)
        else:
            octet = 0
            while True:
                start = self.take(8, "a presence octet of %s" % nd.name)
                if start is None:
                    if octet:  # ruling 21 of 0.8: a continuation octet at the end of the extent
                        raise self.short("a continuation octet of presence %s" % nd.name)
                    break
                for k in range(7):
                    if msg.uint(start + k, 1):
                        bits.add(octet * 7 + k + 1)
                if not msg.uint(start + 7, 1):
                    break
                octet += 1
        rest = None
        for bit in bits:
            if bit not in nd.named:  # ruling 1 of 0.8
                raise MalformedWire(
                    "presence bit %s.%d is 1 and no 'if' block names it" % (nd.name, bit), format=self.pack.name
                )
            if bit not in nd.named_static:  # named only inside cases: counts when such a case is read
                if rest is None:
                    rest = set()
                rest.add(bit)
        if rest is not None:
            self.readings.append((nd, rest))
            self.pending.setdefault(nd, []).append(rest)
        self.env[nd] = bits

    def check_named(self):
        """0.20: a presence bit that is 1 and that no `if` of the lines read names is `malformed-wire`."""
        for nd, rest in self.readings:
            if rest:
                raise MalformedWire(
                    "presence bit %s.%d is 1 and no 'if' block of the cases read names it" % (nd.name, min(rest)),
                    format=self.pack.name,
                )

    def chain(self, nd, sc):
        parts = nd.parts
        last = len(parts) - 1
        for i, part in enumerate(parts):
            self.run(part, sc)
            if i == last and not nd.ends_fx:
                return
            start = self.take(1, "an 'fx' bit of a chain")
            if start is None or not self.msg.uint(start, 1):
                return
        raise MalformedWire(  # ruling 3 of 0.8
            "the chain ends with an 'fx' line and its bit is 1", format=self.pack.name
        )

    def length(self, nd, sc):
        start = self.take(nd.length, "a length of %d bit(s)" % nd.length)
        if start is None:
            end = self.pos  # the block is beyond its enclosing extent: every line of it reads as absent
        else:
            count = self.msg.uint(start, nd.length) - nd.plus
            if count < 0:
                raise MalformedWire(
                    "a length states %d, less than its 'plus %d'" % (count + nd.plus, nd.plus), format=self.pack.name
                )
            end = self.pos + (count * 8 if nd.unit == "bytes" else count)
            if end > self.limit():
                raise self.short("the block a length states")
        self.soft.append(end)
        self.units.append(None if start is None else nd.unit)
        try:
            self.run(nd.body, sc)
        finally:
            self.soft.pop()
            self.units.pop()
        self.pos = end  # content of the extent no line reads is skipped

    def whole(self, nd):
        start = self.take(nd.length, "the length of the message")
        if start is None:
            return
        total = (self.msg.uint(start, nd.length) - nd.plus) * 8
        if total > self.hard:
            raise MalformedWire("the input is shorter than the length it states", format=self.pack.name)
        if total < self.pos:  # ruling 8 of 0.8
            raise MalformedWire("the stated length is shorter than what was already read", format=self.pack.name)
        self.hard = total
        soft = self.soft
        for i in range(len(soft)):
            if soft[i] > total:
                soft[i] = total

    def over(self, nd, n):
        return MalformedWire(
            "list %s has %d item(s); the limit on list items allows %d more" % (nd.list, n, max(self.left, 0)),
            format=self.pack.name,
        )

    def replaces(self, nd, sc):
        """The group is on the wire: the items of an earlier group of its list,
        which it replaces (ruling 33 of 0.8), are no longer held (ruling 7g of 0.11)."""
        old = sc.lists.get(nd.list)
        if old and self.left is not None:
            self.left += _held(old)

    def count(self, nd):
        """0.21: a count line; below its `plus` is `malformed-wire`, checked here."""
        start = self.take(nd.length, "the count %s" % nd.name)
        if start is None:  # at or past the end of a length extent: its group is absent
            self.counts[nd] = None
            return
        v = self.msg.uint(start, nd.length)
        if v < nd.plus:
            raise MalformedWire("count %s states %d, less than its 'plus %d'" % (nd.name, v, nd.plus),
                                format=self.pack.name)
        self.counts[nd] = v - nd.plus

    def group(self, nd, sc):
        mode = nd.mode
        msg = self.msg
        items = []
        if mode == "count" and nd.count_node is not None:  # 0.21: the count read earlier
            n = self.counts.get(nd.count_node)
            if n is None or (self.soft and self.pos >= self.soft[-1]):
                return  # ruling 6: the count was not read, or the group is past the extent's end
            self.replaces(nd, sc)
            if self.left is not None and n > self.left:  # before any item is read (ruling 7d of 0.11)
                raise self.over(nd, n)
            if n > self.limit() - self.pos:  # ruling 20: the bits left at the group
                raise self.short("the %d item(s) of group %s" % (n, nd.list))
            for i in range(n):
                items.append(self.item(nd, sc, i))
        elif mode == "rest":  # 0.21: items while bits remain in the extent
            if self.units and self.units[-1] is None:
                return  # the block is not read: the list stays absent (ruling 3)
            self.replaces(nd, sc)
            end = self.limit()
            least = 1 if self.units and self.units[-1] == "bits" else 8
            while end - self.pos >= least:
                at = self.pos
                items.append(self.item(nd, sc, len(items)))
                if self.pos == at:  # ruling 1
                    raise MalformedWire("an item of group %s reads no bits" % nd.list, format=self.pack.name)
            if self.pos < end:
                self.pos = end  # padding, whatever its value (ruling 17)
        elif mode == "last":  # 0.21: each item is preceded by one bit, 1 on the last
            start = self.take(1, "the bit before an item of group %s" % nd.list)
            if start is None:
                return  # ruling 34 of 0.8: the first indicator at the extent end, an absent list
            self.replaces(nd, sc)
            while True:
                items.append(self.item(nd, sc, len(items)))
                if msg.uint(start, 1):
                    break
                start = self.take(1, "the bit before an item of group %s" % nd.list)
                if start is None:  # ruling 13
                    raise self.short("the bit before item %d of group %s" % (len(items), nd.list))
        elif mode == "count" or mode == "size":
            if mode == "size":
                if self.at_soft_end():
                    return
                what = "the count of group %s" % nd.list
                n = self.constrained(nd.lb, nd.ub, what) if nd.ub is not None else self.length_det(what)
            else:
                start = self.take(nd.length, "the count of group %s" % nd.list)
                if start is None:
                    return
                n = msg.uint(start, nd.length)
            self.replaces(nd, sc)
            if self.left is not None and n > self.left:  # before any item is read (ruling 7d of 0.11)
                raise self.over(nd, n)
            if n > self.limit() - self.pos:  # ruling 14 of 0.8
                raise self.short("the %d item(s) of group %s" % (n, nd.list))
            for i in range(n):
                items.append(self.item(nd, sc, i))
        elif mode == "while":
            while True:
                start = self.take(1, "the bit before an item of group %s" % nd.list)
                if start is None:
                    if not items:
                        return
                    break
                if not items:
                    self.replaces(nd, sc)
                if not msg.uint(start, 1):
                    break
                items.append(self.item(nd, sc, len(items)))
        elif mode == "more":
            while True:
                start = self.take(1, "the bit before an item of group %s" % nd.list)
                if start is None and not items:
                    return  # the group starts at the end of the extent: an absent list (ruling 7)
                if not items:
                    self.replaces(nd, sc)
                # ruling 21 of 0.8: an indicator at the end of the extent reads
                # as 0, so the item it stands before is the last one
                items.append(self.item(nd, sc, len(items)))
                if start is None or not msg.uint(start, 1):
                    break
        else:  # fx
            if self.soft and self.pos >= self.soft[-1]:
                return
            self.replaces(nd, sc)
            while True:
                items.append(self.item(nd, sc, len(items)))
                start = self.take(1, "the bit after an item of group %s" % nd.list)
                if start is None or not msg.uint(start, 1):
                    break
        sc.lists[nd.list] = items

    def item(self, nd, sc, i):
        if self.left is not None:
            if self.left < 1:
                raise self.over(nd, i + 1)
            self.left -= 1
        item = Item()
        isc = _Scope(item.fields, item.lists, "%s%s[%d]." % (sc.prefix, nd.list, i))
        kind = self.body(nd.scope, isc)
        if kind is not None:
            item.kind = (self.pack.name, kind.name)
        return item

    def body(self, body, sc):
        """Decode one message or one item: the common lines, then the first
        kind whose `when` lines, read where the common lines ended, all hold.
        -> the kind selected, or None for a body with no kinds."""
        self.run(body.common, sc)
        selected = None
        if body.kinds:
            mark = self.pos
            for kind in body.kinds:
                for w in kind.whens:
                    if not self.when_holds(w):
                        break
                else:
                    selected = kind
                    break
                self.pos = mark
            else:
                raise NoKind("the %s matches no kind" % ("item" if sc.prefix else "message"), format=self.pack.name)
            self.run(selected.nodes, sc)
        self.settle(sc)
        return selected


def decode(pack, data, limit=None):
    """-> Entity. Input beyond the last line is ignored (ruling 9 of 0.8).
    `limit` is the limit on list items (ruling 7 of 0.11)."""
    rd = _Reader(pack, data, limit)
    ent = Entity()
    kind = rd.body(pack.seq, _Scope(ent.fields, ent.lists, ""))
    rd.check_named()
    if kind is not None:
        ent.kind = (pack.name, kind.name)
    if rd.failures:
        raise TransformDecodeFailure(
            "%d field(s) could not be decoded" % len(rd.failures), format=pack.name, entity=ent, absent=rd.failures
        )
    return ent


# --- encode ----------------------------------------------------------------


class _Ctx:
    """What one message or one item is written from. `readback` holds what
    decode would read for each key from the lines written so far (0.20), and
    `token` names the scope: None for the message, the item's id for an item."""

    __slots__ = ("fields", "lists", "defaults", "readback", "token")

    def __init__(self, fields, lists, defaults, readback=None, token=None):
        self.fields = fields
        self.lists = lists
        self.defaults = defaults
        self.readback = {} if readback is None else readback
        self.token = token

    def inside(self, case):
        """The context of the lines of one case: its defaults over the scope's (rulings 7, 22)."""
        if not case.defaults:
            return self
        return _Ctx(self.fields, self.lists, case.defaults_over(self.defaults), self.readback, self.token)


def select_kind(pack, body, kind_ref, fields, lists, item=False):
    """The kind a message or an item is written as: the one it recorded when
    it was decoded from this format, else the first whose `requires` keys and
    lists it has. None for a body with no kinds. An item whose recorded kind
    names this format and a kind its group does not have selects by `requires`
    (ruling 26 of 0.8)."""
    if not body.kinds:
        return None
    if kind_ref is not None and kind_ref[0] == pack.name:
        kind = body.kind(kind_ref[1])
        if kind is not None:
            return kind
        if not item:
            raise NoKind("the format has no kind %r" % (kind_ref[1],), format=pack.name)
    from .engine import requires_hold

    for kind in body.kinds:
        if requires_hold(kind, fields, lists):
            return kind
    raise NoKind("the entity has the required keys of no kind", format=pack.name)


def _live(bindings, ctx):
    """The bindings of a field line that may write: a line with `with` whose
    key the scope does not hold is absent everywhere (0.18, ruling 1)."""
    out = None
    for k, f in enumerate(bindings):
        w = f.with_key
        if w is not None and w not in ctx.fields and not ctx.lists.get(w):
            if out is None:
                out = list(bindings[:k])
        elif out is not None:
            out.append(f)
    return bindings if out is None else out


class _Writer:
    def __init__(self, pack, known=None):
        self.pack = pack
        self.ops = []     # (start, length, value)
        self.pos = 0
        self.env = {}     # presence node -> the set of its bits that are 1
        self.wholes = []  # (start, node) of each `length ... whole`
        self.blocks = {}  # id(item) -> (ctx, lines), so an item selects its kind once
        # 0.20: the case of each switch, per scope, (token, id(switch)) -> Case or None. A
        # presence, `opt` or `fx` bit is written before the lines it stands for, so the
        # case is foreseen (`used`); the lines then select it (`actual`). When the two
        # differ the message is written again with what was selected (`known`).
        self.known = known or {}
        self.used = {}
        self.actual = {}
        # 0.21: each count line written, [start, node, value], filled in when its
        # group is laid out and checked when the message is finished (ruling 30);
        # the latest of each count node; the open extents, innermost last, each
        # [first bit, unit, (first bit of a `rest` item, node) ...] (rulings 4, 21, 34)
        self.count_recs = []
        self.count_cur = {}
        self.extents = [[0, "bytes", []]]
        self.rest_last = []  # per `rest` item being written: it is its list's last (ruling 10)

    def mismatch(self):
        actual = self.actual
        for k, case in self.used.items():
            if k in actual and actual[k] is not case:
                return True
        return False

    def foresee(self, nd, ctx):
        """The case switch `nd` selects in scope `ctx`, before its lines are written."""
        k = (ctx.token, id(nd))
        if k in self.actual:
            return self.actual[k]
        if k in self.known:
            case = self.known[k]
        else:  # what the first line of the key would write, read back
            case = nd.other
            for line in nd.key_lines:
                got = {}
                try:
                    out, raw = self.line_out(line, ctx)
                except GTError:
                    continue
                self.read_back(line, raw, got)
                if nd.key in got and got[nd.key] is not _FAILED:
                    case = select_case(nd, got[nd.key])
                    break
        self.used[k] = case
        return case

    def put(self, length, value):
        if value:
            self.ops.append((self.pos, length, value))
        self.pos += length

    def fail(self, msg, nd):
        return TransformEncodeFailure(msg, format=self.pack.name, locator=nd.locator)

    def item_block(self, nd, item):
        """-> (ctx, lines) of one item of a group: the common lines and those of its kind."""
        got = self.blocks.get(id(item))
        if got is None or got[2] is not nd:
            body = nd.scope
            kind = select_kind(self.pack, body, getattr(item, "kind", None), item.fields, item.lists, item=True)
            ctx = _Ctx(item.fields, item.lists, body.defaults_for(kind), token=id(item))
            got = self.blocks[id(item)] = (ctx, kind.block if kind is not None else body.common, nd, item)
        return got[0], got[1]

    # -- what a block would write --

    def has_content(self, nodes, ctx):
        """True when the lines would write a present key (a key with a default
        counts, ruling 2 of 0.8, except in a UPER pack, ruling 19 of 0.19) or a
        non-empty list, looked for in every nested block (ruling 3 of 0.19)."""
        defaults = () if self.pack.uper else ctx.defaults
        for nd in nodes:
            t = nd.t
            if t == T_FIELD:
                for f in _live(nd.bindings, ctx):
                    if f.key in ctx.fields or f.key in defaults:
                        return True
            elif t == T_GROUP:
                if ctx.lists.get(nd.list):
                    return True
            elif t == T_SWITCH:  # the selected case only (0.20)
                case = self.foresee(nd, ctx)
                if case is not None and self.has_content(case.nodes, ctx.inside(case)):
                    return True
            else:
                for part in children(nd):
                    if self.has_content(part, ctx):
                        return True
        return False

    def presence_bits(self, p, nodes, ctx, bits):
        """Add to `bits` the bits of presence `p` that some `if` block of
        `nodes` names without `!` and has content for (ruling 4 of 0.8). An
        `if` inside a group is asked once per item: any item with the content
        sets the bit (ruling 12 of 0.8)."""
        for nd in nodes:
            t = nd.t
            if t == T_IF:
                named = [bit for q, bit, neg in nd.conds if q is p and not neg]
                if named and self.has_content(nd.body, ctx):
                    bits.update(named)
                self.presence_bits(p, nd.body, ctx, bits)
            elif t == T_OPT or t == T_LENGTH or t == T_CHAIN or t == T_EXT or t == T_CHOICE:
                for part in children(nd):
                    self.presence_bits(p, part, ctx, bits)
            elif t == T_SWITCH:  # the selected case only (0.20)
                case = self.foresee(nd, ctx)
                if case is not None:
                    self.presence_bits(p, case.nodes, ctx.inside(case), bits)
            elif t == T_GROUP:
                for item in ctx.lists.get(nd.list) or ():
                    ictx, lines = self.item_block(nd, item)
                    self.presence_bits(p, lines, ictx, bits)

    # -- lines --

    def emit(self, nodes, ctx, block=None):
        """Write the lines. `block` is the whole block they stand in when it is
        more than `nodes` (a chain's parts): where a presence is looked for."""
        for nd in nodes:
            t = nd.t
            if t == T_FIELD:
                self.field(nd, ctx)
            elif t == T_CONST or t == T_WHEN:
                self.put(nd.length, nd.value)
            elif t == T_PAD:
                self.pos += nd.length
            elif t == T_ALIGN:
                self.pos += (-self.pos) % nd.length
            elif t == T_PRESENCE:
                bits = set()
                self.presence_bits(nd, block if block is not None else nodes, ctx, bits)
                self.env[nd] = bits
                start = self.pos
                if nd.length is not None:
                    self.pos += nd.length
                else:  # trailing octets that are all zero are not written, and at least one octet is
                    octets = (max(bits) + 6) // 7 if bits else 1
                    for k in range(octets - 1):
                        self.ops.append((start + 8 * k + 7, 1, 1))
                    self.pos += 8 * octets
                for bit in bits:
                    at = bit - 1 if nd.length is not None else (bit - 1) // 7 * 8 + (bit - 1) % 7
                    self.ops.append((start + at, 1, 1))
            elif t == T_IF:
                env = self.env
                for p, bit, neg in nd.conds:
                    if (bit in env.get(p, _NONE)) == neg:
                        break
                else:
                    self.emit(nd.body, ctx)
            elif t == T_OPT:
                if self.has_content(nd.body, ctx):
                    self.put(1, 1)
                    self.emit(nd.body, ctx)
                else:
                    self.pos += 1
            elif t == T_GROUP:
                self.group(nd, ctx)
            elif t == T_COUNT:  # written when its group is laid out; an absent list writes `n`
                rec = [self.pos, nd, nd.plus]
                self.count_recs.append(rec)
                self.count_cur[nd] = rec
                self.pos += nd.length
            elif t == T_CHAIN:
                parts = nd.parts
                whole = [ln for part in parts for ln in part] if len(parts) > 1 else None
                for i, part in enumerate(parts):
                    self.emit(part, ctx, whole)
                    if i == len(parts) - 1:
                        if nd.ends_fx:
                            self.pos += 1  # the chain ends with an `fx` line: written 0
                        break
                    if not any(self.has_content(later, ctx) for later in parts[i + 1:]):
                        self.pos += 1
                        break
                    self.put(1, 1)
            elif t == T_LENGTH:
                at = self.pos
                self.pos += nd.length
                start = self.pos
                self.extents.append([start, nd.unit, []])
                self.emit(nd.body, ctx)
                size = self.pos - start
                if nd.unit == "bytes":  # padded with zero bits to a whole byte
                    self.pos += (-size) % 8
                    size = (self.pos - start) // 8
                self.check_extent(self.extents.pop(), self.pos)
                stated = size + nd.plus
                if not 0 <= stated < (1 << nd.length):
                    raise self.fail("a length of %d does not fit its %d bit(s)" % (stated, nd.length), nd)
                if stated:
                    self.ops.append((at, nd.length, stated))
            elif t == T_EXT:  # the extension bit 0, then the root components (0.19)
                self.pos += 1
                self.emit(nd.body, ctx)
            elif t == T_CHOICE:  # the first alt with content, else the first alt (0.19)
                for alt in nd.alts:
                    if self.has_content(alt.body, ctx):
                        break
                else:
                    alt = nd.alts[0]
                if nd.ext:
                    self.pos += 1
                self.put_constrained(0, nd.n - 1, alt.value)
                self.emit(alt.body, ctx)
            elif t == T_SWITCH:
                rb = ctx.readback
                if rb.get(nd.key, _MISSING) is _FAILED:  # ruling 31 of 0.20
                    raise TransformEncodeFailure(
                        "the key of 'switch %s' does not read back from the bits written" % nd.key,
                        format=self.pack.name, locator=nd.key,
                    )
                case = select_case(nd, rb.get(nd.key, _MISSING))
                self.actual[(ctx.token, id(nd))] = case
                if case is None:
                    raise _no_case(self.pack, nd, ctx.token is not None)
                self.emit(case.nodes, ctx.inside(case))
            else:  # whole: written when the message is complete
                self.wholes.append((self.pos, nd))
                self.pos += nd.length

    # -- UPER (0.19) --

    def put_constrained(self, lb, ub, v):
        self.put((ub - lb).bit_length(), v - lb)

    def put_length(self, n, nd, what):
        """A length determinant; 16384 or more would be fragmented: `transform-encode`."""
        if n < 128:
            self.put(8, n)
        elif n < 16384:
            self.put(16, 0x8000 | n)
        else:
            raise self.fail("%s: a length of %d needs a fragmented length determinant" % (what, n), nd)

    def put_normally_small(self, n, nd, what):
        if n <= 63:
            self.put(7, n)
        else:
            k = uint_octets(n)
            self.put(1, 1)
            self.put_length(k, nd, what)
            self.put(8 * k, n)

    def put_int64(self, v, nd, what):
        if not -(1 << 63) <= v <= INT64_MAX:
            raise self.fail("%s: %d does not fit a signed 64-bit integer" % (what, v), nd)

    def uper_absent(self, nd):
        """An absent key with no default (ruling 1 of 0.19)."""
        loc = nd.uper
        form = loc.form
        if form == "int" and loc.ub is None:
            self.put(8, 1)  # one octet of 0: `lb` for `int:<lb>:`, 0 for `int::`
            self.pos += 8
            return
        if loc.ext:
            self.pos += 1
        if form == "int" or form == "enum":
            self.put_constrained(loc.lb, loc.ub, loc.lb)
            return
        n = loc.lb  # `lb` characters of code 0, or `lb` zero octets
        if loc.ub is not None:
            self.put_constrained(loc.lb, loc.ub, n)
        else:
            self.put_length(n, nd, nd.locator)
        self.pos += n * (7 if form == "chars" else 8)

    def uper_put(self, nd, v, f):
        loc = nd.uper
        form = loc.form
        what = "field %s (%s)" % (f.key, nd.locator)
        if form == "int":
            if loc.ub is not None:
                if loc.lb <= v <= loc.ub:
                    if loc.ext:
                        self.pos += 1
                    self.put_constrained(loc.lb, loc.ub, v)
                    return
                if not loc.ext:
                    raise self.fail("%s: %d is outside %d..%d" % (what, v, loc.lb, loc.ub), nd)
                self.put_int64(v, nd, what)
                k = twos_octets(v)
                self.put(1, 1)
                self.put_length(k, nd, what)
                self.put(8 * k, v & ((1 << (8 * k)) - 1))
                return
            self.put_int64(v, nd, what)
            if loc.lb is None:
                k = twos_octets(v)
                self.put_length(k, nd, what)
                self.put(8 * k, v & ((1 << (8 * k)) - 1))
                return
            if v < loc.lb:
                raise self.fail("%s: %d is below %d" % (what, v, loc.lb), nd)
            u = v - loc.lb
            k = uint_octets(u)
            self.put_length(k, nd, what)
            self.put(8 * k, u)
            return
        if form == "enum":
            if v < 0:
                raise self.fail("%s: a negative index %d" % (what, v), nd)
            if v < loc.n:
                if loc.ext:
                    self.pos += 1
                self.put_constrained(0, loc.n - 1, v)
                return
            if not loc.ext:
                raise self.fail("%s: index %d is outside the %d root values" % (what, v, loc.n), nd)
            self.put(1, 1)
            self.put_normally_small(v - loc.n, nd, what)
            return
        n = len(v)
        if loc.ub is not None:
            if not loc.lb <= n <= loc.ub:
                raise self.fail("%s: a count of %d is outside %d..%d" % (what, n, loc.lb, loc.ub), nd)
            self.put_constrained(loc.lb, loc.ub, n)
        else:
            self.put_length(n, nd, what)
        if form == "chars":
            acc = 0
            for c in v:
                acc = (acc << 7) | c
            self.put(7 * n, acc)
        else:
            self.put(8 * n, int.from_bytes(v, "big"))

    def field(self, nd, ctx):
        """Write one field line and note what decode would read from it."""
        out, raw = self.line_out(nd, ctx)
        length = nd.length
        if out is _UPER_ABSENT:  # a UPER locator: the value of ruling 1 of 0.19
            self.uper_absent(nd)
        elif isinstance(out, _UperValue):
            self.uper_put(nd, out.v, out.f)
        elif isinstance(out, _UntilCodes):  # 0.22: the characters, then the terminator
            t = nd.until
            for c in out.codes:
                self.put(t.width, c)
            self.put(t.width, t.code)
        elif out is None:
            self.pos += length
        elif isinstance(out, (bytes, bytearray)):
            start = self.pos
            for k, b in enumerate(out):
                if b:
                    self.ops.append((start + 8 * k, 8, b))
            self.pos = start + length
        elif isinstance(out, tuple):  # the six-bit values of a `sixbit` field (0.14)
            start = self.pos
            for k, c in enumerate(out):
                if c:
                    self.ops.append((start + 6 * k, 6, c))
            self.pos = start + length
        else:
            self.put(length, out)
        self.read_back(nd, raw, ctx.readback)

    def read_back(self, nd, raw, got):
        """Add to `got` the value decode reads for each key of the line from
        `raw` (None: a sentinel), as `_Reader.field` does (0.20)."""
        if raw is None:
            return
        first = nd.bindings[0]
        if first.absent is not None:  # as `_Reader.field` compares it, `sixbit` too
            if raw_int(raw, self.pack.bit_order == "msb") in first.absent:
                return
        n2c = self.pack.native_to_cot
        for f in nd.bindings:
            key = f.key
            if key in got and got[key] is not _FAILED:
                continue
            try:
                value = f.transform.decode(raw)
                if f.via_dec is not None:
                    value = _via_decode(f, value)
            except ValueError:
                got.setdefault(key, _FAILED)  # a later line of the key may still read back
                continue
            if n2c and key == "type" and value in n2c:
                value = n2c[value]
            got[key] = value

    def line_out(self, nd, ctx):
        """-> (what the line writes, what decode reads back) of one field
        line: the value of the earliest key of the line that is present, else
        the earliest `default` among them, else the line's first sentinel
        (0.9), else zero bits. A sentinel is never content (ruling 4 of 0.9).
        What is written is None (zero bits), an integer or bytes, as they are
        put, or for a UPER locator (0.19) `_UPER_ABSENT` or a `_UperValue`,
        which `field` writes; what is read back is None for a sentinel. Nothing
        is written here: a switch foresees its case through this (0.20)."""
        length = nd.length
        fields = ctx.fields
        bindings = _live(nd.bindings, ctx)
        for f in bindings:
            if f.key in fields:
                value = fields[f.key]
                break
        else:
            defaults = ctx.defaults
            for f in bindings:
                if f.key in defaults:
                    value = defaults[f.key]
                    break
            else:
                first = nd.bindings[0]
                if nd.uper is not None:  # never a sentinel (ruling 22 of 0.19)
                    return _UPER_ABSENT, _uper_absent_raw(nd.uper)
                if nd.until is not None:  # the terminator alone (0.22, ruling 10)
                    return _UntilCodes(()), []
                if first.absent_out is None or not bindings or bindings[0] is not first:  # ruling 20 of 0.18
                    if getattr(first.transform, "as_bytes", False):
                        return None, bytes(length // 8)
                    if getattr(first.transform, "as_sixbit", False):
                        return None, [0] * (length // 6)
                    return None, 0
                # the integer into the field's bits, then `swap`
                v = first.absent_out & ((1 << length) - 1)
                return (_swap_int(v, length) if first.swap else v), None
        try:
            c2n = self.pack.cot_to_native
            if c2n and f.key == "type" and value in c2n:
                value = c2n[value]
            if f.via_enc is not None:
                value = _via_encode(f, value)
            v = f.transform.encode(value)
        except ValueError as e:
            raise TransformEncodeFailure(str(e), format=self.pack.name, locator=f.locator)
        if nd.uper is not None:  # the value as the UPER locator reads it back (0.19)
            return _UperValue(v, f), v
        if nd.until is not None:  # the codes, then the terminator (0.22)
            return _UntilCodes(v), list(v)
        if isinstance(v, (bytes, bytearray)):
            written = bytes(v[::-1] if f.swap else v)
            n = length // 8
            back = (written + bytes(n))[:n]
            return written, (back[::-1] if f.swap else back)
        if isinstance(v, tuple):  # the six-bit values of a `sixbit` field (0.14)
            return v, list(v)
        v = int(v) & ((1 << length) - 1)
        return (_swap_int(v, length) if f.swap else v), v

    def check_extent(self, ext, end):
        """0.21 rulings 4, 21 and 34: an item of a `rest` group that starts with
        fewer than 8 bits left (1 in a block counted in bits) before the padded
        end of its extent would not be read back."""
        least = 1 if ext[1] == "bits" else 8
        for at, nd in ext[2]:
            if end - at < least:
                raise self.fail("an item of list %s starts %d bit(s) before the end of its extent; decode would "
                                "not read it" % (nd.list, end - at), nd)

    def group(self, nd, ctx):
        items = ctx.lists.get(nd.list) or ()
        n = len(items)
        mode = nd.mode
        if mode == "rest":  # 0.21: back to back; an empty or absent list writes nothing
            if nd.tail_rest and self.rest_last and not self.rest_last[-1]:  # ruling 10
                raise self.fail("list %s ends an item of a 'rest' group that is not the last: decode would read the "
                                "items after it into the list" % nd.list, nd)
            ext = self.extents[-1]
            for i, item in enumerate(items):
                at = self.pos
                ictx, lines = self.item_block(nd, item)
                self.rest_last.append(i == n - 1)
                self.emit(lines, ictx)
                self.rest_last.pop()
                if self.pos == at:  # ruling 29
                    raise self.fail("an item of list %s writes no bits" % nd.list, nd)
                ext[2].append((at, nd))
            return
        if mode == "last":  # 0.21: one bit before each item, 1 on the last
            if not n:
                raise self.fail("list %s is absent or empty; a 'last' group has at least one item on the wire"
                                % nd.list, nd)
            for i, item in enumerate(items):
                self.put(1, 1 if i == n - 1 else 0)
                ictx, lines = self.item_block(nd, item)
                self.emit(lines, ictx)
            return
        if mode == "count" and nd.count_node is not None:  # 0.21: the count line holds items + `plus`
            rec = self.count_cur.get(nd.count_node)
            if rec is not None:
                rec[2] = n + nd.count_node.plus
            for item in items:
                ictx, lines = self.item_block(nd, item)
                self.emit(lines, ictx)
            return
        if mode == "size":  # 0.19
            if nd.list not in ctx.lists and nd.lb:  # absent: `lb` items with no keys
                items = [Item() for _ in range(nd.lb)]
            n = len(items)
            if nd.ub is not None:
                if not nd.lb <= n <= nd.ub:
                    raise self.fail("list %s has %d item(s), outside its size %d..%d" % (nd.list, n, nd.lb, nd.ub),
                                    nd)
                self.put_constrained(nd.lb, nd.ub, n)
            else:
                self.put_length(n, nd, "list %s" % nd.list)
        elif mode == "count":  # an absent list writes a count of 0 (ruling 14 of 0.7)
            if n >= (1 << nd.length):
                raise self.fail("list %s has %d item(s), which does not fit the %d-bit count"
                                % (nd.list, n, nd.length), nd)
            self.put(nd.length, n)
        elif mode != "while" and not n:  # rulings 6 of 0.8
            raise self.fail("list %s is absent or empty; a '%s' group has at least one item on the wire"
                            % (nd.list, mode), nd)
        for i, item in enumerate(items):
            if mode == "while":
                self.put(1, 1)
            elif mode == "more":
                self.put(1, 1 if i < n - 1 else 0)
            ictx, lines = self.item_block(nd, item)
            self.emit(lines, ictx)
            if mode == "fx":
                self.put(1, 1 if i < n - 1 else 0)
        if mode == "while":
            self.pos += 1  # the 0 bit that ends the list

    def finish(self):
        """A message is a whole number of bytes: the rest of the last byte is
        zero. Under `armor sixbit` it is the bits up to the cursor, as armored
        text (0.14); no `length ... whole` line is allowed there."""
        # 0.21: the counts, filled in by their groups (ruling 30), and the items of
        # `rest` groups against the end of the message (rulings 4, 21 and 34)
        for at, nd, v in self.count_recs:
            if not 0 <= v < (1 << nd.length):
                raise self.fail("count %s holds %d, which does not fit its %d bit(s)" % (nd.name, v, nd.length), nd)
            if v:
                self.ops.append((at, nd.length, v))
        if self.pack.armor == "sixbit":
            nbits = self.pos
            # the message stays counted in bytes for `rest` (ruling 43 of 0.21)
            self.check_extent(self.extents[0], -(-nbits // 8) * 8)
            acc = 0
            for start, length, v in self.ops:
                acc |= (v & ((1 << length) - 1)) << (nbits - start - length)
            return armored_text(acc, nbits)
        nbytes = -(-self.pos // 8)
        self.check_extent(self.extents[0], nbytes * 8)
        if nbytes == 0 and self.pack.uper:  # an encoding of no bits is one octet 00 (X.691 10.1.3)
            nbytes = 1
        for at, nd in self.wholes:
            stated = nbytes + nd.plus
            if not 0 <= stated < (1 << nd.length):
                raise self.fail("the message length %d does not fit its %d bit(s)" % (stated, nd.length), nd)
            self.ops.append((at, nd.length, stated))
        nbits = nbytes * 8
        msb = self.pack.bit_order != "lsb"
        rev = self.pack.bit_order == "msb-lsb"  # 0.22: placed as `msb`, valued as `lsb`
        acc = 0
        for start, length, v in self.ops:
            v &= (1 << length) - 1
            if rev:
                v = reverse_bits(v, length)
            acc |= v << (nbits - start - length if msb else start)
        return acc.to_bytes(nbytes, "big" if msb else "little").hex()


def encode(pack, entity):
    """-> lowercase hex, or armored text under `armor sixbit`."""
    body = pack.seq
    kind = select_kind(pack, body, entity.kind, entity.fields, entity.lists)
    known = {}
    tries = 8
    for attempt in range(tries):
        wr = _Writer(pack, known)
        last = attempt == tries - 1
        try:
            wr.emit(kind.block if kind is not None else body.common,
                    _Ctx(entity.fields, entity.lists, body.defaults_for(kind)))
            out = wr.finish()
        except GTError:
            if last or not wr.mismatch():
                raise
            out = None
        if not wr.mismatch():
            return out
        if last:  # ruling 33 of 0.20
            raise TransformEncodeFailure("the predicted cases of the switches never settle", format=pack.name)
        known = dict(known)
        known.update(wr.actual)
