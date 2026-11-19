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
"""

from .entity import Entity, Item, _via_decode, _via_encode
from .errors import MalformedWire, NoKind, TransformDecodeFailure, TransformEncodeFailure
from .seqdef import (
    T_ALIGN, T_CHAIN, T_CONST, T_FIELD, T_GROUP, T_IF, T_LENGTH, T_OPT, T_PAD, T_PRESENCE, T_WHEN, T_WHOLE,
)
from .wire_bits import Message, _hex_to_bytes, _swap_int

_NONE = frozenset()


# --- decode ----------------------------------------------------------------


class _Scope:
    """What one message or one item decodes into."""

    __slots__ = ("fields", "lists", "prefix", "failed")

    def __init__(self, fields, lists, prefix):
        self.fields = fields
        self.lists = lists
        self.prefix = prefix
        self.failed = None  # [(key, locator, reason, final)]


class _Reader:
    def __init__(self, pack, data):
        buf = _hex_to_bytes(data, pack.name)
        self.pack = pack
        self.msg = Message(pack, buf)
        self.pos = 0
        self.hard = self.msg.nbits
        self.soft = []      # the ends of the open `length` blocks, outermost first
        self.env = {}       # presence node -> the set of its bits that are 1
        self.failures = []  # (key, locator, reason)

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
            elif t == T_CHAIN:
                self.chain(nd, sc)
            elif t == T_LENGTH:
                self.length(nd, sc)
            else:
                self.whole(nd)

    def when_holds(self, nd):
        """A `when` whose bits lie beyond the input does not hold."""
        pos = self.pos
        if pos + nd.length > self.limit():
            return False
        self.pos = pos + nd.length
        return self.msg.uint(pos, nd.length) == nd.value

    def field(self, nd, sc):
        length = nd.length
        start = self.take(length, "a field of %d bit(s)" % length)
        if start is None:
            return
        msg = self.msg
        n2c = self.pack.native_to_cot
        first = nd.bindings[0]
        as_bytes = getattr(first.transform, "as_bytes", False)
        if as_bytes:
            raw = bytes(msg.uint(start + 8 * k, 8) for k in range(length // 8))
            if first.swap:
                raw = raw[::-1]
        else:
            raw = msg.uint(start, length)
            if first.swap:
                raw = _swap_int(raw, length)
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
        for bit in bits:
            if bit not in nd.named:  # ruling 1 of 0.8
                raise MalformedWire(
                    "presence bit %s.%d is 1 and no 'if' block names it" % (nd.name, bit), format=self.pack.name
                )
        self.env[nd] = bits

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
        try:
            self.run(nd.body, sc)
        finally:
            self.soft.pop()
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

    def group(self, nd, sc):
        mode = nd.mode
        msg = self.msg
        items = []
        if mode == "count":
            start = self.take(nd.length, "the count of group %s" % nd.list)
            if start is None:
                return
            n = msg.uint(start, nd.length)
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
                if not msg.uint(start, 1):
                    break
                items.append(self.item(nd, sc, len(items)))
        elif mode == "more":
            while True:
                start = self.take(1, "the bit before an item of group %s" % nd.list)
                if start is None and not items:
                    return  # the group starts at the end of the extent: an absent list (ruling 7)
                # ruling 21 of 0.8: an indicator at the end of the extent reads
                # as 0, so the item it stands before is the last one
                items.append(self.item(nd, sc, len(items)))
                if start is None or not msg.uint(start, 1):
                    break
        else:  # fx
            if self.soft and self.pos >= self.soft[-1]:
                return
            while True:
                items.append(self.item(nd, sc, len(items)))
                start = self.take(1, "the bit after an item of group %s" % nd.list)
                if start is None or not msg.uint(start, 1):
                    break
        sc.lists[nd.list] = items

    def item(self, nd, sc, i):
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


def decode(pack, data):
    """-> Entity. Input beyond the last line is ignored (ruling 9 of 0.8)."""
    rd = _Reader(pack, data)
    ent = Entity()
    kind = rd.body(pack.seq, _Scope(ent.fields, ent.lists, ""))
    if kind is not None:
        ent.kind = (pack.name, kind.name)
    if rd.failures:
        raise TransformDecodeFailure(
            "%d field(s) could not be decoded" % len(rd.failures), format=pack.name, entity=ent, absent=rd.failures
        )
    return ent


# --- encode ----------------------------------------------------------------


class _Ctx:
    """What one message or one item is written from."""

    __slots__ = ("fields", "lists", "defaults")

    def __init__(self, fields, lists, defaults):
        self.fields = fields
        self.lists = lists
        self.defaults = defaults


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
    for kind in body.kinds:
        for key in kind.requires_keys:
            if key not in fields:
                break
        else:
            for name in kind.requires_lists:
                if not lists.get(name):
                    break
            else:
                return kind
    raise NoKind("the entity has the required keys of no kind", format=pack.name)


class _Writer:
    def __init__(self, pack):
        self.pack = pack
        self.ops = []     # (start, length, value)
        self.pos = 0
        self.env = {}     # presence node -> the set of its bits that are 1
        self.wholes = []  # (start, node) of each `length ... whole`
        self.blocks = {}  # id(item) -> (ctx, lines), so an item selects its kind once

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
            ctx = _Ctx(item.fields, item.lists, body.defaults_for(kind))
            got = self.blocks[id(item)] = (ctx, kind.block if kind is not None else body.common, nd, item)
        return got[0], got[1]

    # -- what a block would write --

    def has_content(self, nodes, ctx):
        """True when the lines would write a present key (a key with a default
        counts, ruling 2 of 0.8) or a non-empty list."""
        for nd in nodes:
            t = nd.t
            if t == T_FIELD:
                for f in nd.bindings:
                    if f.key in ctx.fields or f.key in ctx.defaults:
                        return True
            elif t == T_GROUP:
                if ctx.lists.get(nd.list):
                    return True
            elif t == T_IF or t == T_OPT or t == T_LENGTH:
                if self.has_content(nd.body, ctx):
                    return True
            elif t == T_CHAIN:
                for part in nd.parts:
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
            elif t == T_OPT or t == T_LENGTH:
                self.presence_bits(p, nd.body, ctx, bits)
            elif t == T_CHAIN:
                for part in nd.parts:
                    self.presence_bits(p, part, ctx, bits)
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
                self.emit(nd.body, ctx)
                size = self.pos - start
                if nd.unit == "bytes":  # padded with zero bits to a whole byte
                    self.pos += (-size) % 8
                    size = (self.pos - start) // 8
                stated = size + nd.plus
                if not 0 <= stated < (1 << nd.length):
                    raise self.fail("a length of %d does not fit its %d bit(s)" % (stated, nd.length), nd)
                if stated:
                    self.ops.append((at, nd.length, stated))
            else:  # whole: written when the message is complete
                self.wholes.append((self.pos, nd))
                self.pos += nd.length

    def field(self, nd, ctx):
        """The value of the earliest key of the line that is present, else the
        earliest `default` among them, else zero bits."""
        length = nd.length
        fields = ctx.fields
        for f in nd.bindings:
            if f.key in fields:
                value = fields[f.key]
                break
        else:
            defaults = ctx.defaults
            for f in nd.bindings:
                if f.key in defaults:
                    value = defaults[f.key]
                    break
            else:
                self.pos += length
                return
        try:
            c2n = self.pack.cot_to_native
            if c2n and f.key == "type" and value in c2n:
                value = c2n[value]
            if f.via_enc is not None:
                value = _via_encode(f, value)
            v = f.transform.encode(value)
        except ValueError as e:
            raise TransformEncodeFailure(str(e), format=self.pack.name, locator=f.locator)
        if isinstance(v, (bytes, bytearray)):
            if f.swap:
                v = v[::-1]
            start = self.pos
            for k, b in enumerate(v):
                if b:
                    self.ops.append((start + 8 * k, 8, b))
            self.pos = start + length
        else:
            v = int(v) & ((1 << length) - 1)
            self.put(length, _swap_int(v, length) if f.swap else v)

    def group(self, nd, ctx):
        items = ctx.lists.get(nd.list) or ()
        n = len(items)
        mode = nd.mode
        if mode == "count":  # an absent list writes a count of 0 (ruling 14 of 0.7)
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
        """A message is a whole number of bytes: the rest of the last byte is zero."""
        nbytes = -(-self.pos // 8)
        for at, nd in self.wholes:
            stated = nbytes + nd.plus
            if not 0 <= stated < (1 << nd.length):
                raise self.fail("the message length %d does not fit its %d bit(s)" % (stated, nd.length), nd)
            self.ops.append((at, nd.length, stated))
        nbits = nbytes * 8
        msb = self.pack.bit_order == "msb"
        acc = 0
        for start, length, v in self.ops:
            acc |= (v & ((1 << length) - 1)) << (nbits - start - length if msb else start)
        return acc.to_bytes(nbytes, "big" if msb else "little").hex()


def encode(pack, entity):
    """-> lowercase hex."""
    body = pack.seq
    kind = select_kind(pack, body, entity.kind, entity.fields, entity.lists)
    wr = _Writer(pack)
    wr.emit(kind.block if kind is not None else body.common,
            _Ctx(entity.fields, entity.lists, body.defaults_for(kind)))
    return wr.finish()
