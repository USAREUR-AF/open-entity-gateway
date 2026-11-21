"""The `tlv` wire (spec/engine-spec.md, Version 0.13): a set of items, each a
tag, a length and a value, a value possibly a set itself; an optional fixed
key and length before the set and an optional `sum16` checksum item at its
end. Input and output are hex text.

Decode reads the top set whole (`parse`), checks the checksum, and leaves the
nested sets unread: a `when` reads into them leniently while the kind is
selected (a set that does not parse makes the `when` not hold), then `walk`
reads every set a line of the selected scope walks into, strictly. Encode
builds the sets from the lines of the scope in line order, each nested set at
the position of the first line that names it or a path under it, then the
residue of each set in input order."""

import math
import re
import struct

from .errors import MalformedWire, TransformEncodeFailure
from .transforms import _build_scale, _finite_number, _finite_wire, _pack_single
from .octets import HexT, TlvTransform, Utf8T, hex_to_bytes as _hex_to_bytes  # shared with `bits` (0.19)

MAX_TAG = (1 << 28) - 1
DIRECTIVES = ("tag", "length", "key", "checksum")
TAG_FORMS = ("ber-oid", "1", "2", "4")
LENGTH_FORMS = ("ber", "1", "2", "4")
TRANSFORM_NAMES = ("uint", "twos", "f32", "f64", "utf8", "hex")  # with `uscale:` and `sscale:`

_TAG = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_KEY = re.compile(r"(?:[0-9a-f]{2}){1,64}\Z")
_HEX_TEXT = re.compile(r"(?:[0-9a-fA-F]{2})*\Z")


def is_tlv_only_name(name):
    return name in ("utf8", "hex")


def read_tag(text):
    """A tag as the pack writes it: a decimal integer from 0 to 2^28-1 with no
    leading zero. -> int, or None."""
    if not (text.isascii() and _TAG.match(text)):
        return None
    n = int(text)
    return n if n <= MAX_TAG else None


def valid_key(text):
    return text.isascii() and _KEY.match(text) is not None


def tag_limit(tag_form):
    """-> one more than the largest tag the `tag` encoding holds."""
    return MAX_TAG + 1 if tag_form == "ber-oid" else 1 << (8 * int(tag_form))


def parse_path(locator):
    """`<tag>[.<tag>...][:<n>]` -> (tags, size or None). Raises ValueError."""
    path, colon, size = locator.partition(":")
    if colon:
        if not (len(size) == 1 and "1" <= size <= "8"):  # ruling 6: one digit from 1 to 8
            raise ValueError("a size is one digit from 1 to 8, got %r" % size)
        size = int(size)
    else:
        size = None
    tags = []
    for part in path.split("."):
        t = read_tag(part)
        if t is None:
            raise ValueError(
                "a tlv path is tags separated by '.', each a decimal integer from 0 to 2^28-1 with no leading zero;"
                " got %r" % locator
            )
        tags.append(t)
    return tuple(tags), size


# --- transforms ---------------------------------------------------------------


class ParamError(ValueError):
    """A scale transform whose parameters do not parse."""


def _integer(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s transform needs an integer, got %s" % (name, type(value).__name__))
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("%s transform got non-integral %r" % (name, value))
        value = int(value)
    return value


def _int_bytes(name, raw):
    if len(raw) > 8:
        raise ValueError("%s: a value of %d bytes; an integer value is 1 to 8 bytes" % (name, len(raw)))
    return int.from_bytes(raw, "big")


def _signed(raw, v):
    bits = 8 * len(raw)
    return v - (1 << bits) if v >> (bits - 1) else v


class UintT(TlvTransform):
    name = "uint"
    yields = "int"

    def __init__(self, size):
        self.size = size

    def decode(self, raw):
        v = _int_bytes(self.name, raw)
        if len(raw) == 8 and v >> 63:  # ruling 2 of 0.5, whatever the size
            v -= 1 << 64
        return v

    def encode(self, value):
        v = _integer(self.name, value)
        lo = -(1 << 63) if self.size == 8 else 0  # ruling 3: ruling 2 of 0.5 covers only an 8-byte uint
        if v < lo or v >= 1 << (8 * self.size):
            raise ValueError("uint: %d does not fit %d byte(s)" % (v, self.size))
        return (v & ((1 << (8 * self.size)) - 1)).to_bytes(self.size, "big")


class TwosT(TlvTransform):
    name = "twos"
    yields = "int"

    def __init__(self, size):
        self.size = size

    def decode(self, raw):
        return _signed(raw, _int_bytes(self.name, raw))

    def encode(self, value):
        v = _integer(self.name, value)
        half = 1 << (8 * self.size - 1)
        if v < -half or v >= half:
            raise ValueError("twos: %d does not fit %d byte(s)" % (v, self.size))
        return (v & ((1 << (8 * self.size)) - 1)).to_bytes(self.size, "big")


class ScaleT(TlvTransform):
    """`uscale` / `sscale`: the value as for `uint` (unsigned whatever its
    length, ruling 4) or `twos`, then as on `bits` (ruling 1 of 0.5); encode as
    on `bits`, into `size` bytes."""

    yields = "float"

    def __init__(self, name, size):
        try:
            self.bits = _build_scale(name, 8 * size)
        except ValueError as e:
            raise ParamError(str(e))
        self.name = name
        self.size = size

    def decode(self, raw):
        v = _int_bytes(self.name.split(":")[0], raw)
        if self.bits.signed:
            v = _signed(raw, v)
        b = self.bits
        x = float(v)
        x = x * b.fraction[0] / b.fraction[1] if b.fraction is not None else x * b.factor
        return x + b.offset

    def encode(self, value):
        return self.bits.encode(value).to_bytes(self.size, "big")


class F32T(TlvTransform):
    name = "f32"
    yields = "float"

    def decode(self, raw):
        if len(raw) != 4:
            raise ValueError("f32: a value of %d bytes, not 4" % len(raw))
        return _finite_wire("f32", struct.unpack(">f", raw)[0])

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("f32 transform needs a number, got %s" % type(value).__name__)
        return _pack_single("f32", ">f", value)


class F64T(TlvTransform):
    name = "f64"
    yields = "float"

    def decode(self, raw):
        if len(raw) != 8:
            raise ValueError("f64: a value of %d bytes, not 8" % len(raw))
        return _finite_wire("f64", struct.unpack(">d", raw)[0])

    def encode(self, value):
        x = _finite_number("f64", value)
        if not math.isfinite(x):
            raise ValueError("f64: %r is not finite" % value)
        return struct.pack(">d", x)


def build_transform(name, size):
    """-> the transform of a `field` line. Raises ParamError for scale
    parameters that do not parse, ValueError (`bad-directive`) for a name not
    in the table or a size missing, given or out of range."""
    if name.startswith(("uscale:", "sscale:")):
        ScaleT(name, 8)  # the parameters first: ParamError
        if size is None:
            raise ValueError("transform %s needs a size ':<n>'" % name.split(":")[0])
        return ScaleT(name, size)
    if name in ("uint", "twos"):
        if size is None:
            raise ValueError("transform %s needs a size ':<n>'" % name)
        return UintT(size) if name == "uint" else TwosT(size)
    if name == "f32" or name == "f64":
        want = 4 if name == "f32" else 8
        if size is not None and size != want:
            raise ValueError("transform %s takes no size or ':%d', got ':%d'" % (name, want, size))
        return F32T() if name == "f32" else F64T()
    if name == "utf8" or name == "hex":
        if size is not None:
            raise ValueError("transform %s takes no size" % name)
        return Utf8T() if name == "utf8" else HexT()
    raise ValueError("transform %r is not a transform of the tlv wire" % name)


# --- load checks --------------------------------------------------------------


def check_pack(tag_form, checksum, checksum_line, common, kinds, deferred, source, name):
    """The checks of a `tlv` pack that need the whole file: every tag under a
    fixed `tag <n>`, a line whose first tag is the checksum tag, and a line at
    a path under another line's path in one scope (ruling 5). Each failure is
    `bad-directive`, appended to `deferred` as (lineno, error)."""
    from .errors import BadDirective

    def bad(lineno, msg, locator=None):
        deferred.append((lineno, BadDirective("%s, at %s:%d" % (msg, source, lineno), format=name, locator=locator)))

    limit = tag_limit(tag_form)
    if checksum is not None and checksum >= limit:
        bad(checksum_line, "the checksum tag %d does not fit 'tag %s'" % (checksum, tag_form), "checksum")
    lines = [ln for ln in common if not ln.is_group]
    for k in kinds:
        lines.extend(ln for ln in k.lines if not ln.is_group)
    for ln in lines:
        tags = ln.loc[0]
        if any(t >= limit for t in tags):
            bad(ln.lineno, "a tag of the path does not fit 'tag %s'" % tag_form, ln.locator)
        if checksum is not None and tags[0] == checksum:
            bad(ln.lineno, "the path begins with the checksum tag %d" % checksum, ln.locator)

    def scope(lines):
        first = {}
        for ln in lines:
            first.setdefault(ln.loc[0], ln.lineno)
        for ln in lines:
            tags = ln.loc[0]
            for d in range(1, len(tags)):
                at = first.get(tags[:d])
                if at is not None:
                    bad(max(at, ln.lineno), "a line at a path under the path of another line of its scope",
                        ln.locator)

    base = [ln for ln in common if not ln.is_group]
    scope(base)
    for k in kinds:
        scope(base + [ln for ln in k.lines if not ln.is_group])


# --- decode -------------------------------------------------------------------


class _Bad(Exception):
    pass


def _read_tag(pack, buf, pos, end):
    form = pack.tlv_tag
    if form == "ber-oid":
        if pos >= end:
            raise _Bad("a tag runs past its set")
        if buf[pos] == 0x80:
            raise _Bad("a BER-OID tag whose first byte is 0x80")
        v = 0
        for _ in range(4):
            if pos >= end:
                raise _Bad("a tag runs past its set")
            b = buf[pos]
            pos += 1
            v = (v << 7) | (b & 0x7F)
            if not b & 0x80:
                return v, pos
        raise _Bad("a BER-OID tag of more than four bytes")
    n = int(form)
    if end - pos < n:
        raise _Bad("a tag runs past its set")
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def _read_length(pack, buf, pos, end):
    """-> (length, position after it). The length is not compared here."""
    form = pack.tlv_length
    if pos >= end:
        raise _Bad("a length runs past its set")
    if form == "ber":
        b = buf[pos]
        pos += 1
        if b < 0x80:
            return b, pos
        if b == 0x80 or b > 0x88:
            raise _Bad("a BER length whose first byte is 0x%02x" % b)
        n = b - 0x80
    else:
        n = int(form)
    if end - pos < n:
        raise _Bad("a length runs past its set")
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def _items(pack, buf, pos, end):
    """The items of the set buf[pos:end] -> [(tag, start, end)]. Raises _Bad."""
    out = []
    while pos < end:
        tag, pos = _read_tag(pack, buf, pos, end)
        n, pos = _read_length(pack, buf, pos, end)
        if n > end - pos:  # compared with the bytes left, never added first
            raise _Bad("item %d states %d byte(s) of value where its set has %d left" % (tag, n, end - pos))
        out.append((tag, pos, pos + n))
        pos += n
    return out


class TSet:
    """One read set: its items in order, the index of each tag's first
    occurrence, and the nested sets read so far (a set, or the text of why it
    does not parse)."""

    __slots__ = ("buf", "items", "first", "children")

    def __init__(self, buf, items):
        self.buf = buf
        self.items = items
        first = {}
        for i, it in enumerate(items):
            first.setdefault(it[0], i)
        self.first = first
        self.children = {}

    def value(self, i):
        _, s, e = self.items[i]
        return self.buf[s:e]

    def child(self, pack, i, strict, path):
        c = self.children.get(i)
        if c is None:
            _, s, e = self.items[i]
            try:
                c = TSet(self.buf, _items(pack, self.buf, s, e))
            except _Bad as err:
                c = str(err)
            self.children[i] = c
        if isinstance(c, str):
            if strict:
                raise MalformedWire("the set at %s: %s" % (_path_text(path), c), format=pack.name, locator=_path_text(path))
            return None
        return c


class Message:
    __slots__ = ("top", "walked")

    def __init__(self, top):
        self.top = top
        self.walked = {}  # path -> TSet, in the order the lines walk into them


def _path_text(path):
    return ".".join(str(t) for t in path)


def parse(pack, data):
    """Steps 1, 2, the reading of the top set in step 3, and step 4 of
    "Reading a message"."""
    fmt = pack.name
    buf = _hex_to_bytes(data, fmt)
    pos, end = 0, len(buf)
    try:
        key = pack.tlv_key
        if key is not None:
            if buf[:len(key)] != key:
                raise _Bad("the input does not begin with the key %s" % key.hex())
            n, pos = _read_length(pack, buf, len(key), end)
            if n > end - pos:
                raise _Bad("the set states %d byte(s) where the input has %d" % (n, end - pos))
            end = pos + n
        items = _items(pack, buf, pos, end)
    except _Bad as e:
        raise MalformedWire(str(e), format=fmt)
    t = pack.tlv_checksum
    if t is not None:
        if not items or items[-1][0] != t or items[-1][2] - items[-1][1] != 2:
            raise MalformedWire("the last item of the set is not a checksum item of tag %d and two bytes" % t,
                                format=fmt)
        for it in items[:-1]:
            if it[0] == t:
                raise MalformedWire("an item of the checksum tag %d before the last" % t, format=fmt)
        start = items[-1][1]
        want = sum16(buf, start)
        have = int.from_bytes(buf[start:start + 2], "big")
        if have != want:
            raise MalformedWire("the checksum is %04x; the bytes sum to %04x" % (have, want), format=fmt)
        items = items[:-1]
    return Message(TSet(buf, items))


def sum16(buf, end):
    """Bytes 0 to end-1: a byte at an even offset counts b * 256, at an odd offset b; modulo 65536."""
    return (sum(buf[0:end:2]) * 256 + sum(buf[1:end:2])) & 0xFFFF


def when_holds(pack, msg, when):
    tags, _ = when.loc
    node = msg.top
    for t in tags[:-1]:
        i = node.first.get(t)
        if i is None:
            return False
        node = node.child(pack, i, False, ())
        if node is None:  # a value that is not a well-formed set
            return False
    i = node.first.get(tags[-1])
    if i is None:
        return False
    if when.present:
        return True
    v = node.value(i)
    return 1 <= len(v) <= 8 and int.from_bytes(v, "big") == when.value


def walk(pack, msg, plan):
    """Step 3, after the kind: read whole every set a line of the scope walks
    into (ruling 7: every set on the path of a `field`, `when` or `const`)."""
    walked = msg.walked
    for ln in plan.lines:
        if ln.is_group:
            continue
        tags = ln.loc[0]
        node = msg.top
        for d in range(len(tags) - 1):
            i = node.first.get(tags[d])
            if i is None:
                break
            path = tags[:d + 1]
            node = node.child(pack, i, True, path)
            walked.setdefault(path, node)


def get(pack, msg, f):
    tags = f.loc[0]
    node = msg.top if len(tags) == 1 else msg.walked.get(tags[:-1])
    if node is None:
        return None
    i = node.first.get(tags[-1])
    if i is None:
        return None
    v = node.value(i)
    if not v and not f.transform.empty_ok:
        return None
    return v


def items(pack, msg, group):
    return None  # `group` is `bad-directive` on `tlv` in 0.13


def _named(plan):
    """-> {set path: the tags a line of the scope names in it}. `when ...
    present` does not name the last tag of its path (ruling 7)."""
    named = plan.cache.get("tlv")
    if named is None:
        named = {}  # built whole, then kept with the plan (spec 0.25, rulings 1a and 1c)
        for ln in plan.lines:
            if ln.is_group:
                continue
            tags = ln.loc[0]
            last = len(tags) - 1
            for d, t in enumerate(tags):
                if d == last and not ln.is_field and ln.present:
                    continue
                named.setdefault(tags[:d], set()).add(t)
        plan.cache["tlv"] = named
    return named


def residue(pack, msg, plan):
    """-> [(path, value as hex)]: every item of the top set and of each set
    the scope walks into whose tag no line names there, every occurrence, in
    input order (Residue)."""
    named = _named(plan)
    out = []
    for path, node in [((), msg.top)] + list(msg.walked.items()):
        names = named.get(path, ())
        buf = node.buf
        for t, s, e in node.items:
            if t not in names:
                out.append((_path_text(path + (t,)), buf[s:e].hex()))
    return out


# --- encode -------------------------------------------------------------------


class _Node:
    __slots__ = ("slots", "residue")

    def __init__(self):
        self.slots = {}    # tag -> _Slot, in the order of the first line naming it
        self.residue = []  # (tag, value bytes), in input order


class _Slot:
    __slots__ = ("lines", "set")

    def __init__(self):
        self.lines = []  # (line index, line) of the lines at this path
        self.set = None  # the nested set, for lines at paths under it


def _encode_tag(pack, tag):
    form = pack.tlv_tag
    if form == "ber-oid":
        if not 0 <= tag <= MAX_TAG:
            raise TransformEncodeFailure("tag %d does not fit a BER-OID tag of four bytes" % tag, format=pack.name)
        out = [tag & 0x7F]
        tag >>= 7
        while tag:
            out.append(0x80 | (tag & 0x7F))
            tag >>= 7
        return bytes(reversed(out))
    n = int(form)
    if not 0 <= tag < 1 << (8 * n):
        raise TransformEncodeFailure("tag %d does not fit %d byte(s)" % (tag, n), format=pack.name)
    return tag.to_bytes(n, "big")


def _encode_length(pack, n):
    form = pack.tlv_length
    if form == "ber":
        if n < 0x80:
            return bytes((n,))
        k = (n.bit_length() + 7) // 8
        if k > 8:
            raise TransformEncodeFailure("a length of %d does not fit a BER length" % n, format=pack.name)
        return bytes((0x80 | k,)) + n.to_bytes(k, "big")
    k = int(form)
    if n >= 1 << (8 * k):
        raise TransformEncodeFailure("a length of %d does not fit %d byte(s)" % (n, k), format=pack.name)
    return n.to_bytes(k, "big")


def _item(pack, tag, value):
    return _encode_tag(pack, tag) + _encode_length(pack, len(value)) + value


def _residue_entries(residue):
    pairs = residue.items() if isinstance(residue, dict) else residue
    for entry in pairs or ():
        try:
            path, text = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(path, str) or not isinstance(text, str) or _HEX_TEXT.match(text) is None:
            continue
        tags = []
        for part in path.split("."):
            t = read_tag(part)
            if t is None:
                break
            tags.append(t)
        else:
            yield tuple(tags), bytes.fromhex(text)


def overridden(plan):
    """-> the indexes of the `field` lines whose path a numeric `when` or a
    `const` of the scope also names: the item holds the earliest such value
    and the field is not encoded (ruling 9)."""
    skip = plan.cache.get("tlv-skip")
    if skip is None:
        fixed = {ln.loc[0] for ln in plan.lines if not ln.is_field and not ln.is_group and not ln.present}
        skip = plan.cache["tlv-skip"] = {i for i, f in plan.ifields if f.loc[0] in fixed}
    return skip


def write(pack, enc, residue):
    """Steps 1 to 3 of "Writing a message" -> lowercase hex text."""
    plan = enc.plan
    root = _Node()
    for li, ln in enumerate(plan.lines):
        if ln.is_group:
            continue
        tags = ln.loc[0]
        node = root
        last = len(tags) - 1
        for d, t in enumerate(tags):
            slot = node.slots.get(t)
            if slot is None:
                slot = node.slots[t] = _Slot()
            if d < last:
                if slot.set is None:
                    slot.set = _Node()
                node = slot.set
            else:
                slot.lines.append((li, ln))
    for tags, value in _residue_entries(residue):
        node = root
        for t in tags[:-1]:
            slot = node.slots.get(t)
            if slot is None or slot.set is None or slot.lines:
                node = None  # no set of the scope holds it: not written
                break
            node = slot.set
        if node is not None:
            node.residue.append((tags[-1], value))
    values = enc.values

    def serialize(node):
        out = bytearray()
        held = {t for t, _ in node.residue}
        for t, slot in node.slots.items():
            if not slot.lines:
                inner = serialize(slot.set)
                if inner:  # a nested set with no items is not written
                    out += _item(pack, t, bytes(inner))
                continue
            value = None
            for _, ln in slot.lines:  # a `when` or `const` value stands over a field's
                if not ln.is_field and not ln.present:
                    value = ln.value.to_bytes(ln.loc[1], "big")
                    break
            if value is None:
                for li, ln in slot.lines:  # the earliest present key (ruling 16 of 0.5)
                    if ln.is_field and li in values:
                        value = values[li]
                        break
            if value is None and t not in held and any(not ln.is_field and ln.present for _, ln in slot.lines):
                value = b""  # `when ... present`: an empty item, unless residue holds one (ruling 2)
            if value is not None:
                out += _item(pack, t, bytes(value))
        for t, value in node.residue:
            out += _item(pack, t, value)
        return out

    body = serialize(root)
    t = pack.tlv_checksum
    if t is not None:
        body += _encode_tag(pack, t) + _encode_length(pack, 2) + b"\x00\x00"
    out = bytearray()
    if pack.tlv_key is not None:
        out += pack.tlv_key + _encode_length(pack, len(body))
    out += body
    if t is not None:
        out[-2:] = sum16(out, len(out) - 2).to_bytes(2, "big")
    return out.hex()
