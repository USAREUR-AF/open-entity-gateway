"""The transform set.

Text transforms (version 0.1): string, float, int, iso8601_ms. A text transform
decodes wire text to a typed model value and encodes it back; `kind` is "string"
or "number" so a typed wire (JSON) knows how to write it, an untyped wire (XML)
uses the text alone.

Bits transforms (version 0.2): uint, twos, linscale. A bits transform decodes a
field's raw unsigned bits (an integer) to a typed model value and encodes a model
value back to the low `len` bits of an integer. Each bits transform is built per
field, bound to that field's bit length (and linscale's maxpos/range), so no
caller has to pass the length. Bits transforms are valid only on a `bits` wire,
text transforms only on a text wire (spec/engine-spec.md).

Version 0.5 adds `bool` (the `json` wire only) and the bits transforms `uscale`,
`sscale`, `f32`, `f64` and `ascii`; Version 0.14 adds `sixbit`. A transform reports a failure as ValueError;
the engine names it `transform-decode` or `transform-encode`.
"""

import math
import re
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# The grammars of ruling 2 of 0.11, ASCII digits only. 2a: integer text.
_INT_TEXT = re.compile(r"-?[0-9]+\Z")
# 2b: number text.
_NUM_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
# 2c: an unsigned decimal integer.
_UINT_TEXT = re.compile(r"[0-9]+\Z")
_I64_MIN, _I64_MAX = -(1 << 63), (1 << 63) - 1
# Ruling 3 of 0.11: the grammar of `iso8601_ms`.
_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(?:[Zz]|([+-])([0-9]{2}):([0-9]{2}))\Z"
)
_ISO_MIN = -62135596800000  # 0001-01-01T00:00:00.000Z
_ISO_MAX = 253402300799999  # 9999-12-31T23:59:59.999Z
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)


def format_float(value):
    """Shortest round-trip decimal; an integer-valued float has no decimal point.

    Integer-valued floats are written as the shortest round-trip digits expanded
    without an exponent (1e300 -> "1" followed by 300 zeros), never the exact
    binary expansion.
    """
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite float %r" % value)
    if value.is_integer():
        if value == 0:
            return "0"
        return format(Decimal(repr(value)), "f").split(".")[0]
    return format(Decimal(repr(value)), "f")  # plain notation, never an exponent


def format_int(value):
    return str(int(value))


def read_int_text(text):
    """Integer text (ruling 2a of 0.11): an optional `-`, then ASCII digits,
    within a signed 64-bit integer. Raises ValueError."""
    if not _INT_TEXT.match(text):
        raise ValueError("not integer text: %r" % text[:40])
    if len(text.lstrip("-").lstrip("0")) > 19:
        raise ValueError("integer out of range: %r" % text[:40])
    n = int(text)
    if n < _I64_MIN or n > _I64_MAX:
        raise ValueError("integer out of range: %r" % text[:40])
    return n


def read_number_text(text):
    """Number text (ruling 2b of 0.11) -> the nearest double. Text whose
    nearest double is infinite is refused; `-` and a value that reads as zero
    is -0.0. Raises ValueError."""
    if not _NUM_TEXT.match(text):
        raise ValueError("not number text: %r" % text[:40])
    v = float(text)  # correctly rounded, ties to even
    if not math.isfinite(v):
        raise ValueError("number out of range: %r" % text[:40])
    return v


def read_uint_text(text, bits=None):
    """An unsigned decimal integer (ruling 2c of 0.11): ASCII digits only,
    leading zeros allowed; with `bits`, below 2^bits. Raises ValueError."""
    if not _UINT_TEXT.match(text):
        raise ValueError("not an unsigned decimal integer: %r" % text[:40])
    if bits is not None and len(text.lstrip("0")) > len(str(1 << bits)):
        raise ValueError("%r does not fit %d bit(s)" % (text[:40], bits))
    n = int(text)
    if bits is not None and n >= (1 << bits):
        raise ValueError("%r does not fit %d bit(s)" % (text[:40], bits))
    return n


def is_uint_text(text):
    return _UINT_TEXT.match(text) is not None


def read_json_int(text):
    """A JSON number through `int` (ruling 2d of 0.11): its exact value must be
    an integer within a signed 64-bit integer. Raises ValueError."""
    if _INT_TEXT.match(text):
        return read_int_text(text)
    try:
        d = Decimal(text)
    except ArithmeticError:
        raise ValueError("not a number: %r" % text[:40])
    if not d.is_finite():
        raise ValueError("not a number: %r" % text[:40])
    if d.is_zero():
        return 0
    if d.adjusted() > 18:
        raise ValueError("integer out of range: %r" % text[:40])
    if d.adjusted() < 0 or d != d.to_integral_value():
        raise ValueError("not an integer: %r" % text[:40])
    n = int(d)
    if n < _I64_MIN or n > _I64_MAX:
        raise ValueError("integer out of range: %r" % text[:40])
    return n


def read_json_float(text):
    """A JSON number through `float` (ruling 2d of 0.11): the nearest double;
    infinite is refused. Raises ValueError."""
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("number out of range: %r" % text[:40])
    return v


_JSON_INT = re.compile(r"-?[0-9]+\Z")


def canonical_number(raw):
    """Canonical text of a JSON number given its raw text (used for residue)."""
    if _JSON_INT.match(raw):
        return format_int(int(raw))
    return format_float(float(raw))


def round_half_away(x):
    """Round a double to the nearest integer, halves away from zero, exactly
    (ruling 4a of 0.11)."""
    sign = -1 if x < 0 else 1
    ax = abs(x)
    whole = math.floor(ax)
    if ax - whole >= 0.5:  # exact: no `+ 0.5` that could round up a value just below a half
        whole += 1
    return sign * int(whole)


class Transform:
    name = None
    kind = None  # "string" or "number" (text wires); "bits" for a bits transform
    yields = None  # the model type the decode side produces: "string" | "float" | "int"

    def decode(self, text):
        raise NotImplementedError

    def encode(self, value):
        raise NotImplementedError


class StringT(Transform):
    name = "string"
    kind = "string"
    yields = "string"

    def decode(self, text):
        return text

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("string transform needs a str, got %s" % type(value).__name__)
        return value


class FloatT(Transform):
    name = "float"
    kind = "number"
    yields = "float"

    def decode(self, text):
        """Number text (ruling 2b of 0.11): nothing is trimmed."""
        return read_number_text(text)

    def decode_json(self, text):
        """A JSON number (ruling 2d of 0.11)."""
        return read_json_float(text)

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("float transform needs a number, got %s" % type(value).__name__)
        return format_float(value)


class IntT(Transform):
    name = "int"
    kind = "number"
    yields = "int"

    def decode(self, text):
        """Integer text (ruling 2a of 0.11): nothing is trimmed."""
        return read_int_text(text)

    def decode_json(self, text):
        """A JSON number (ruling 2d of 0.11): an integer value, every digit kept."""
        return read_json_int(text)

    def encode(self, value):
        if isinstance(value, bool):
            raise ValueError("int transform needs an int, got bool")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("int transform got non-integral %r" % value)
            value = int(value)
        if not isinstance(value, int):
            raise ValueError("int transform needs an int, got %s" % type(value).__name__)
        return format_int(value)


class Iso8601MsT(Transform):
    """ISO 8601 <-> epoch milliseconds. Decode accepts YYYY-MM-DDTHH:MM:SS[.f+]
    followed by `Z`, `z`, `+hh:mm` or `-hh:mm`, the `T` in either case (0.6);
    the instant is converted to UTC and fraction digits beyond milliseconds are
    truncated. Encode writes YYYY-MM-DDTHH:MM:SS.fffZ."""

    name = "iso8601_ms"
    kind = "string"
    yields = "int"

    def decode(self, text):
        """Ruling 3 of 0.11: the grammar is exact, nothing is trimmed, the
        fraction is cut to milliseconds, and the instant less its offset
        lies in the years 0001 to 9999."""
        m = _ISO.match(text)
        if not m:
            raise ValueError("not ISO 8601 YYYY-MM-DDTHH:MM:SS[.f+] with Z or +hh:mm / -hh:mm: %r" % text[:40])
        y, mo, d, h, mi, s, frac, sign, oh, om = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError("invalid date/time %r: %s" % (text[:40], e))
        ms = int((frac or "")[:3].ljust(3, "0"))
        offset_ms = 0
        if sign is not None:
            if int(oh) > 23 or int(om) > 59:
                raise ValueError("invalid UTC offset in %r" % text[:40])
            offset_ms = (int(oh) * 60 + int(om)) * 60000 * (-1 if sign == "-" else 1)
        value = (dt - _EPOCH) // _MS + ms - offset_ms
        if value < _ISO_MIN or value > _ISO_MAX:
            raise ValueError("the instant of %r lies outside the years 0001 to 9999" % text[:40])
        return value

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ValueError("iso8601_ms needs integer epoch ms, got %r" % (value,))
        if value < _ISO_MIN or value > _ISO_MAX:
            raise ValueError("epoch ms out of range: %r" % value)
        dt = _EPOCH + timedelta(milliseconds=value)
        return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond // 1000,
        )


def _civil(days):
    """Days since 1970-01-01 -> (year, month, day), proleptic Gregorian."""
    z = days + 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y), m, d


class Iso8601ShortT(Iso8601MsT):
    """`iso8601_short` (0.18): decoded as `iso8601_ms`; encoded without a zero
    fraction and with the trailing zeros of the milliseconds removed."""

    name = "iso8601_short"

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ValueError("iso8601_short needs integer epoch ms, got %r" % (value,))
        ms = value - 1000 * (value // 1000)  # ruling 11
        secs = value // 1000
        days, sod = divmod(secs, 86400)
        y, mo, d = _civil(days)
        if y < 0 or y > 9999:
            raise ValueError("epoch ms out of range: %r" % value)
        out = "%04d-%02d-%02dT%02d:%02d:%02d" % (y, mo, d, sod // 3600, sod // 60 % 60, sod % 60)
        if ms:
            out += "." + ("%03d" % ms).rstrip("0")
        return out + "Z"


class BoolT(Transform):
    """`bool` (version 0.5, `json` wire only): JSON `true` / `false` <-> int 1 / 0.
    `decode` takes the parsed JSON boolean; `encode` returns the JSON text."""

    name = "bool"
    kind = "bool"
    yields = "int"

    def decode(self, value):
        if value is True:
            return 1
        if value is False:
            return 0
        raise ValueError("bool transform needs JSON true or false")

    def encode(self, value):
        if not isinstance(value, bool):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if value == 1 and isinstance(value, int):
                return "true"
            if value == 0 and isinstance(value, int):
                return "false"
        raise ValueError("bool transform encodes 1 or 0, got %r" % (value,))


TRANSFORMS = {t.name: t for t in (StringT(), FloatT(), IntT(), Iso8601MsT(), Iso8601ShortT())}
TEXT_TRANSFORM_NAMES = frozenset(TRANSFORMS) | {"bool"}
JSON_TRANSFORMS = dict(TRANSFORMS, bool=BoolT())  # `bool` is valid only on the `json` wire


# --- bits transforms (version 0.2) ------------------------------------------


class BitsTransform(Transform):
    kind = "bits"

    def __init__(self, name, length):
        self.name = name
        self.length = length
        self.mask = (1 << length) - 1

    def _twos(self, bits):
        """Interpret `length` raw bits as a two's-complement signed integer."""
        if bits >= (1 << (self.length - 1)):
            return bits - (1 << self.length)
        return bits


class UintT(BitsTransform):
    yields = "int"

    def decode(self, bits):
        # Raw bits are the unsigned value. At 64 bits a set top bit reads as the
        # negative model integer with the same bit pattern (0.5 ruling 2).
        if self.length == 64 and bits >= (1 << 63):
            return bits - (1 << 64)
        return bits

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("uint transform needs an integer, got %s" % type(value).__name__)
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("uint transform got non-integral %r" % value)
            value = int(value)
        if self.length >= 64:  # every model integer, as its two's-complement pattern (ruling 2 of 0.5)
            lo, hi = _I64_MIN, _I64_MAX
        else:
            lo, hi = 0, self.mask
        _check_range(self.name, value, lo, hi, self.length)
        return value & self.mask


def _check_range(name, value, lo, hi, length):
    """Ruling 4b of 0.11: a value outside the field's range is refused, never wrapped."""
    if value < lo or value > hi:
        raise ValueError(
            "%s: value %d does not fit the %d-bit field (%d..%d)" % (name, value, length, lo, hi)
        )


class TwosT(BitsTransform):
    yields = "int"

    def decode(self, bits):
        return self._twos(bits)

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("twos transform needs an integer, got %s" % type(value).__name__)
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("twos transform got non-integral %r" % value)
            value = int(value)
        _check_range(self.name, value, -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1, self.length)
        return value & self.mask


class LinscaleT(BitsTransform):
    """`linscale:<maxpos>:<range>`: signed two's-complement value `v`, then
    `v / maxpos * range` (float); encode is `round(x / range * maxpos)`."""

    yields = "float"

    def __init__(self, name, length, maxpos, value_range):
        super().__init__(name, length)
        self.maxpos = maxpos
        self.range = value_range

    def decode(self, bits):
        # Ruling 4c of 0.11: the integer becomes the nearest double, then
        # `v / maxpos * range` in doubles.
        return float(self._twos(bits)) / self.fmaxpos * self.range

    @property
    def fmaxpos(self):
        return float(self.maxpos)

    def encode(self, value):
        x = _finite_number("linscale", value)
        q = x / self.range * self.fmaxpos
        if not math.isfinite(q):
            raise ValueError("%s: value %r is out of the field's range" % (self.name, value))
        r = round_half_away(q)
        _check_range(self.name, r, -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1, self.length)
        return r & self.mask


def _finite_number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s transform needs a number, got %s" % (name, type(value).__name__))
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("%s transform got non-finite %r" % (name, value))
    return x


def _finite_wire(name, value):
    """Infinity and NaN on the wire are a decode failure (0.5 ruling 23, extended
    to `f64` and `pdouble` in 0.6): a model float is always finite."""
    if not math.isfinite(value):
        raise ValueError("%s: %r on the wire is not a finite number" % (name, value))
    return value


def _pack_single(name, fmt, value):
    """A finite double narrowed to an IEEE-754 single; a value outside single
    range is an encode failure (0.5 ruling 23)."""
    x = _finite_number(name, value)
    try:
        out = struct.pack(fmt, x)
    except OverflowError:
        raise ValueError("%s value out of single-precision range: %r" % (name, value))
    if not math.isfinite(struct.unpack(fmt, out)[0]):
        raise ValueError("%s value out of single-precision range: %r" % (name, value))
    return out


class ScaleT(BitsTransform):
    """`uscale:<factor>[:<offset>]` / `sscale:<factor>[:<offset>]` (version 0.5).
    Decode: the bits as unsigned (`uscale`) or two's-complement (`sscale`) `v`,
    then `v * factor + offset`; encode `round((x - offset) / factor)`. A factor
    written `<num>/<den>` computes `v * num / den` and `(x - offset) * den / num`,
    in doubles and in that order (0.5 ruling 1). A value outside the field's
    range after rounding is an encode error."""

    yields = "float"

    def __init__(self, name, length, signed, factor, fraction, offset):
        super().__init__(name, length)
        self.signed = signed
        self.factor = factor      # a double, or None when `fraction` is set
        self.fraction = fraction  # (num, den) as doubles, or None
        self.offset = offset
        if signed:
            self.lo, self.hi = -(1 << (length - 1)), (1 << (length - 1)) - 1
        else:
            self.lo, self.hi = 0, (1 << length) - 1

    def decode(self, bits):
        v = float(self._twos(bits) if self.signed else bits)
        if self.fraction is not None:
            scaled = v * self.fraction[0] / self.fraction[1]
        else:
            scaled = v * self.factor
        return scaled + self.offset

    def encode(self, value):
        x = _finite_number(self.name.split(":")[0], value)
        if self.fraction is not None:
            q = (x - self.offset) * self.fraction[1] / self.fraction[0]
        else:
            q = (x - self.offset) / self.factor
        if not math.isfinite(q):
            raise ValueError("%s: value %r is out of the field's range" % (self.name, value))
        r = round_half_away(q)
        if r < self.lo or r > self.hi:
            raise ValueError(
                "%s: value %r rounds to %d, outside the %d-bit field's range %d..%d"
                % (self.name, value, r, self.length, self.lo, self.hi)
            )
        return r & self.mask


class F32T(BitsTransform):
    """32 bits as an IEEE-754 single, widened on decode, narrowed on encode."""

    yields = "float"

    def decode(self, bits):
        return _finite_wire("f32", struct.unpack(">f", bits.to_bytes(4, "big"))[0])

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("f32 transform needs a number, got %s" % type(value).__name__)
        return int.from_bytes(_pack_single("f32", ">f", value), "big")


class F64T(BitsTransform):
    """64 bits as an IEEE-754 double."""

    yields = "float"

    def decode(self, bits):
        return _finite_wire("f64", struct.unpack(">d", bits.to_bytes(8, "big"))[0])

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("f64 transform needs a number, got %s" % type(value).__name__)
        return int.from_bytes(struct.pack(">d", _finite_number("f64", value)), "big")


class AsciiT(BitsTransform):
    """`len / 8` bytes in wire order as text. The wire hands `decode` the bytes
    and takes bytes back from `encode` (`as_bytes`), each byte's bits in the
    pack's bit order. Trailing NUL bytes are removed on decode and added on
    encode; an all-NUL field is the empty string, present (0.5 ruling 3)."""

    yields = "string"
    as_bytes = True
    trim = False  # the `trim` field option (0.6): decode also removes trailing spaces

    def __init__(self, name, length):
        self.name = name
        self.length = length
        self.nbytes = length // 8

    def decode(self, raw):
        body = bytes(raw).rstrip(b"\x00")
        if self.trim:
            body = body.rstrip(b" ")
        for b in body:
            if b > 0x7E or b < 0x20:
                raise ValueError("ascii: byte 0x%02x is outside 0x20..0x7e" % b)
        return body.decode("ascii")

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("ascii transform needs a str, got %s" % type(value).__name__)
        body = value.encode("utf-8")
        for b in body:
            if b > 0x7E or b < 0x20:  # 0.5 ruling 19
                raise ValueError("ascii: byte 0x%02x is outside 0x20..0x7e" % b)
        if len(body) > self.nbytes:
            raise ValueError("ascii: text of %d byte(s) does not fit %d byte(s)" % (len(body), self.nbytes))
        return body + b"\x00" * (self.nbytes - len(body))


class SixbitT(BitsTransform):
    """`len / 6` characters of six bits each (0.14). The wire hands `decode`
    the six-bit values, first character first, each one's six bits read in
    the pack's bit order, and takes such values back from `encode`
    (`as_sixbit`). Values 0 to 31 are `@` to `_`, 32 to 63 space to `?`.
    Trailing `@` are removed on decode (with `trim`, the whole trailing run of
    `@` and space, in any mix) and added on encode; an all-`@` field is the
    empty string, present (ruling 6 of 0.14)."""

    yields = "string"
    as_sixbit = True
    trim = False

    def __init__(self, name, length):
        self.name = name
        self.length = length
        self.nchars = length // 6

    def decode(self, raw):
        text = "".join(chr(0x40 + v) if v < 32 else chr(v) for v in raw)
        return text.rstrip("@ ") if self.trim else text.rstrip("@")

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("sixbit transform needs a str, got %s" % type(value).__name__)
        out = []
        for c in value:
            o = ord(c)
            if not 0x20 <= o <= 0x5F:
                raise ValueError("sixbit: character %r is outside 0x20..0x5f" % c)
            out.append(o - 0x40 if o >= 0x40 else o)
        if len(out) > self.nchars:
            raise ValueError("sixbit: text of %d character(s) does not fit %d" % (len(out), self.nchars))
        out.extend([0] * (self.nchars - len(out)))
        return tuple(out)


def is_bits_transform_name(name):
    return name in ("uint", "twos", "f32", "f64", "ascii", "sixbit") or name.startswith(("linscale:", "uscale:", "sscale:"))


def bits_len_allowed(name, length):
    """Whether a bits transform allows bit length `length` (spec 0.5: `f32` 32,
    `f64` 64, `ascii` a multiple of 8 from 8 to 2040; 0.14: `sixbit` a
    multiple of 6 from 6 to 1530; 0.15: `v15_len_allowed`; the rest 1..=64)."""
    if name == "f32":
        return length == 32
    if name == "f64":
        return length == 64
    if name == "ascii":
        return 8 <= length <= 2040 and length % 8 == 0
    if name == "sixbit":
        return 6 <= length <= 1530 and length % 6 == 0
    if v15_family(name) is not None:
        return v15_len_allowed(name, length)
    return 1 <= length <= 64


def _nonzero_number(text, what):
    """Number text (2b) that does not read as zero (ruling 4d of 0.11)."""
    v = read_number_text(text)
    if v == 0:
        raise ValueError("%s %r reads as zero" % (what, text))
    return v


def scale_params(name):
    """`uscale:<factor>[:<offset>]` and `sscale:...` (ruling 4d of 0.11) ->
    (factor, fraction, offset). The factor is number text that is not zero,
    or `<num>/<den>` of two unsigned decimal integers, neither zero; the
    offset is number text. Raises ValueError (`bad-directive`)."""
    parts = name.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("%s takes the form %s:<factor>[:<offset>]" % (parts[0], parts[0]))
    factor = fraction = None
    if "/" in parts[1]:
        num, _, den = parts[1].partition("/")
        n, d = read_uint_text(num), read_uint_text(den)
        if n == 0 or d == 0:
            raise ValueError("a fraction factor's num and den must be non-zero")
        try:
            fraction = (float(n), float(d))
        except OverflowError:
            raise ValueError("a fraction factor's num or den is beyond a double")
    else:
        factor = _nonzero_number(parts[1], "factor")
    offset = read_number_text(parts[2]) if len(parts) == 3 else 0.0
    return factor, fraction, offset


def linscale_params(name):
    """`linscale:<maxpos>:<range>` (ruling 4d of 0.11) -> (maxpos, range).
    Raises ValueError (`bad-directive`)."""
    parts = name.split(":")
    if len(parts) != 3:
        raise ValueError("linscale takes the form linscale:<maxpos>:<range>")
    maxpos = read_uint_text(parts[1])
    if maxpos < 1:
        raise ValueError("linscale maxpos must be at least 1")
    try:
        float(maxpos)
    except OverflowError:  # no upper bound is ruled; one beyond a double cannot be computed with
        raise ValueError("linscale maxpos is beyond a double")
    return maxpos, _nonzero_number(parts[2], "linscale range")


def _build_scale(name, length):
    factor, fraction, offset = scale_params(name)
    return ScaleT(name, length, name.startswith("sscale"), factor, fraction, offset)


def build_bits_transform(name, length):
    """Return a bits transform bound to `length`. Raises ValueError for an unknown
    or malformed bits transform name."""
    if name == "uint":
        return UintT(name, length)
    if name == "twos":
        return TwosT(name, length)
    if name == "f32":
        return F32T(name, length)
    if name == "f64":
        return F64T(name, length)
    if name == "ascii":
        return AsciiT(name, length)
    if name == "sixbit":
        return SixbitT(name, length)
    if name.startswith(("uscale:", "sscale:")):
        return _build_scale(name, length)
    if name.startswith("linscale:"):
        maxpos, value_range = linscale_params(name)
        return LinscaleT(name, length, maxpos, value_range)
    if v15_family(name) in V15_BITS:
        return build_v15_bits(name, length)
    raise ValueError("unknown bits transform %r" % name)


# Every transform of the specification, by the text before its first `:`
# (ruling 5 of 0.11), and the wires each is valid on.
_TEXTUAL = ("xml", "geojson", "json", "text")
TRANSFORM_WIRES = {
    "string": _TEXTUAL, "float": _TEXTUAL, "int": _TEXTUAL, "iso8601_ms": _TEXTUAL, "bool": ("json",),
    "iso8601_short": ("xml", "geojson", "json"),  # 0.18 (ruling 11: bad-directive elsewhere)
    "uint": ("bits", "tlv"), "twos": ("bits", "tlv"), "linscale": ("bits",), "uscale": ("bits", "tlv"),
    "sscale": ("bits", "tlv"), "f32": ("bits", "tlv"), "f64": ("bits", "tlv"), "ascii": ("bits",),
    "sixbit": ("bits",),  # 0.14
    "utf8": ("tlv",), "hex": ("tlv",),  # 0.13
    "pstring": ("protobuf",), "pvarint": ("protobuf",), "psint": ("protobuf",), "pdouble": ("protobuf",),
    "pfloat": ("protobuf",),
    "dec": ("text",), "dm": ("text",), "hms": ("text",), "hms_dmy": ("text",),
    # 0.15: the value transforms
    "uiscale": ("bits",), "siscale": ("bits",), "smag": ("bits",), "smscale": ("bits",), "f32scale": ("bits",),
    "f64scale": ("bits",), "chars": ("bits",), "octal": ("bits",),
    "scale": ("xml", "geojson", "json"), "iscale": ("xml", "geojson", "json"),
}


class UnknownTransformName(LookupError):
    pass


def check_transform_text(name):
    """Step 7 of ruling 5b of 0.11, on the transform's text alone: its name
    (the text before the first `:`) must be a transform of the specification
    (else UnknownTransformName), and its parameters must be valid (else
    ValueError). -> the name."""
    head, colon, _ = name.partition(":")
    if head not in TRANSFORM_WIRES:
        raise UnknownTransformName(head)
    if head == "linscale":
        linscale_params(name)
    elif head in ("uscale", "sscale"):
        scale_params(name)
    elif v15_family(name) is not None:
        v15_params(name)
    elif head in ("dec", "dm", "hms", "hms_dmy") or (head == "int" and colon):
        from .wire_text import build_transform  # the parameters of the `text` transforms (0.10)

        build_transform(name)
    elif colon:
        raise ValueError("transform %s takes no parameter" % head)
    return head


def transform_valid_on(name, wire):
    """Whether a transform whose text passed `check_transform_text` is valid on `wire`."""
    head, colon, _ = name.partition(":")
    if head == "int" and colon:
        return wire == "text"
    return wire in TRANSFORM_WIRES[head]


# --- protobuf transforms (version 0.3) --------------------------------------

_PB_VARINT, _PB_FIXED64, _PB_LEN, _PB_FIXED32 = 0, 1, 2, 5


class ProtobufTransform(Transform):
    """A protobuf transform fixes the field's protobuf wire type. `decode` takes a
    `(wire_type, raw)` observation from the protobuf wire and interprets it;
    `encode` returns `(wire_type, payload)` for the wire to frame. A wire type on
    the message that does not match the transform's is a decode error
    (spec/engine-spec.md, "Version 0.3"). Valid only on a protobuf wire."""

    kind = "protobuf"
    wire_type = None

    def _check(self, observed):
        if observed != self.wire_type:
            raise ValueError(
                "%s expects protobuf wire type %d, got %d" % (self.name, self.wire_type, observed)
            )


class PStringT(ProtobufTransform):
    name = "pstring"
    yields = "string"
    wire_type = _PB_LEN

    def decode(self, value):
        wt, raw = value
        self._check(wt)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError("pstring: not valid UTF-8: %s" % e)

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("pstring needs a str, got %s" % type(value).__name__)
        return (_PB_LEN, value.encode("utf-8"))


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s needs an integer, got %s" % (name, type(value).__name__))
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("%s got non-integral %r" % (name, value))
        value = int(value)
    return value


class PVarintT(ProtobufTransform):
    name = "pvarint"
    yields = "int"
    wire_type = _PB_VARINT

    def decode(self, value):
        wt, raw = value
        self._check(wt)
        n = int(raw)  # a 64-bit value: the model integer with the same bit pattern (0.5 ruling 15)
        return n - (1 << 64) if n >= (1 << 63) else n

    def encode(self, value):
        return (_PB_VARINT, _require_int("pvarint", value))


class PSintT(ProtobufTransform):
    """Zig-zag varint: small-magnitude signed integers encode small."""

    name = "psint"
    yields = "int"
    wire_type = _PB_VARINT

    def decode(self, value):
        wt, raw = value
        self._check(wt)
        n = int(raw)
        return (n >> 1) ^ -(n & 1)

    def encode(self, value):
        n = _require_int("psint", value)
        return (_PB_VARINT, (n << 1) ^ (n >> 63))


class PDoubleT(ProtobufTransform):
    name = "pdouble"
    yields = "float"
    wire_type = _PB_FIXED64

    def decode(self, value):
        wt, raw = value
        self._check(wt)
        if len(raw) != 8:
            raise ValueError("pdouble needs eight bytes, got %d" % len(raw))
        return _finite_wire("pdouble", struct.unpack("<d", raw)[0])

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("pdouble needs a number, got %s" % type(value).__name__)
        return (_PB_FIXED64, struct.pack("<d", _finite_number("pdouble", value)))


class PFloatT(ProtobufTransform):
    """fixed32 IEEE-754 float, widened to a double on decode, narrowed on encode."""

    name = "pfloat"
    yields = "float"
    wire_type = _PB_FIXED32

    def decode(self, value):
        wt, raw = value
        self._check(wt)
        if len(raw) != 4:
            raise ValueError("pfloat needs four bytes, got %d" % len(raw))
        return _finite_wire("pfloat", struct.unpack("<f", raw)[0])

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("pfloat needs a number, got %s" % type(value).__name__)
        return (_PB_FIXED32, _pack_single("pfloat", "<f", value))


PROTOBUF_TRANSFORMS = {t.name: t for t in (PStringT(), PVarintT(), PSintT(), PDoubleT(), PFloatT())}
PROTOBUF_TRANSFORM_NAMES = frozenset(PROTOBUF_TRANSFORMS)


def is_protobuf_transform_name(name):
    return name in PROTOBUF_TRANSFORM_NAMES


# --- typed text (version 0.6) -------------------------------------------------
# `default` values, value-table entries and caller-supplied keys are text in a
# pack, a vector or on the command line, read as a model type; table lookups
# compare canonical text.


def read_typed(text, type_name):
    """Read `text` as a model type (`string`, `int`, `float`) by the grammars
    of ruling 2 of 0.11 (2a for `int`, 2b for `float`). Raises ValueError."""
    if type_name == "string":
        return text
    if type_name == "int":
        return read_int_text(text)
    return read_number_text(text)


def canonical_text(value, type_name):
    """Canonical text of a model value: a string as-is, an integer as decimal
    digits, a float as in ruling 3 of 0.1.1."""
    if type_name == "string":
        return value
    if type_name == "int":
        return format_int(value)
    return format_float(value)


def table_side_type(values):
    """The type of one side of a value table: `int` when every value is
    integer text (2a), `float` when every one is number text (2b), otherwise
    `string`."""
    if values and all(_INT_TEXT.match(v) for v in values):
        return "int"
    if values and all(_NUM_TEXT.match(v) for v in values):
        return "float"
    return "string"


def parse_factor(text):
    """The parameter of the `scale` derive function (ruling 1 of 0.5, ruling
    2c of 0.11) -> (factor, None) or (None, (num, den)): number text, or
    `<num>/<den>` where `<num>` is integer text (2a) and `<den>` an unsigned
    decimal integer that is not zero. The derive function is not a transform:
    ruling 25 of 0.15 leaves its `<num>` signed. Raises ValueError."""
    if "/" in text:
        num, _, den = text.partition("/")
        n, d = read_int_text(num), read_uint_text(den)
        if d == 0:
            raise ValueError("a fraction factor's den must be non-zero")
        return None, (float(n), float(d))
    return read_number_text(text), None


# --- value transforms (version 0.15) -------------------------------------------
# The integer scales (`uiscale`, `siscale`) and `iscale` read their factor and
# offset as exact numbers and compute with exact rationals (Python integers
# have no width limit). The float scales (`smscale`, `f32scale`, `f64scale`,
# `scale`) read them as `sscale` does and compute in doubles (ruling 1 of 0.5),
# a fraction having an unsigned numerator and denominator (ruling 25).

from fractions import Fraction  # noqa: E402

V15_BITS = frozenset(("uiscale", "siscale", "smag", "smscale", "f32scale", "f64scale", "chars", "octal"))
V15_TEXT = frozenset(("scale", "iscale"))

_EXACT_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")
_EXACT_NUM = re.compile(r"-?[0-9]+\Z")
_EXACT_DEN = re.compile(r"[0-9]+\Z")
_EXACT_OFFSET = re.compile(r"-?[0-9]+\Z")
_ISCALE_TEXT = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?\Z")
_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1
_MAX_ISCALE_EXPONENT = 400
_MAX_FRACTION_DIGITS = 18


def _digits(s):
    """A run of ASCII decimal digits as an integer, of any length (Python's
    limit on converting long digit strings does not apply)."""
    if len(s) <= 4000:
        return int(s)
    n = 0
    for i in range(0, len(s), 4000):
        chunk = s[i:i + 4000]
        n = n * 10 ** len(chunk) + int(chunk)
    return n


def v15_family(name):
    """The transform family of a transform name of version 0.15 (`uiscale`,
    `chars`, `scale`, ...), or None for any other name."""
    head = name.split(":", 1)[0]
    return head if head in V15_BITS or head in V15_TEXT else None


def round_exact(q):
    """round-half-away-from-zero of an exact rational."""
    n, d = q.numerator, q.denominator
    r = (2 * abs(n) + d) // (2 * d)
    return -r if n < 0 else r


def _split_params(name, lo, hi):
    parts = name.split(":")
    if not lo <= len(parts) - 1 <= hi:
        raise ValueError("%s takes %d to %d parameter(s), got %d" % (parts[0], lo, hi, len(parts) - 1))
    return parts


def parse_exact_factor(text):
    """`<factor>` of the integer scales (Exact factors) -> Fraction."""
    if "/" in text:
        num, _, den = text.partition("/")
        if not _EXACT_NUM.match(num) or not _EXACT_DEN.match(den):
            raise ValueError("a fraction factor is [-]<num>/<den> of decimal integers, got %r" % text)
        neg = num.startswith("-")
        n, d = _digits(num.lstrip("-")), _digits(den)
        if d == 0:
            raise ValueError("a fraction factor's den must not be 0")
        f = Fraction(-n if neg else n, d)
    else:
        if not _EXACT_DECIMAL.match(text):
            raise ValueError("factor must be [-]digits[.digits] or <num>/<den>, got %r" % text)
        neg = text.startswith("-")
        whole, _, frac = text.lstrip("-").partition(".")
        f = Fraction(_digits(whole + frac), 10 ** len(frac))
        if neg:
            f = -f
    if f == 0:
        raise ValueError("factor must not be 0")
    if abs(f.numerator) > (1 << 32) or f.denominator > (1 << 32):
        raise ValueError("factor %r in lowest terms has a numerator or denominator above 2^32" % text)
    return f


def parse_exact_offset(text):
    if not _EXACT_OFFSET.match(text):
        raise ValueError("offset must be a decimal integer [-]digits, got %r" % text)
    if len(text.lstrip("-").lstrip("0")) > 19:
        raise ValueError("offset %r is not below 2^63 in absolute value" % text)
    v = int(text)
    if abs(v) >= (1 << 63):
        raise ValueError("offset %r is not below 2^63 in absolute value" % text)
    return v


def parse_float_scale(name):
    """`<name>:<factor>[:<offset>]` of a float scale (`smscale`, `f32scale`,
    `f64scale`, `scale`), read as `sscale` reads it (ruling 12 of 0.15, ruling
    4d of 0.11), the fraction unsigned (ruling 25 of 0.15) -> (factor,
    fraction, offset) as doubles. Raises ValueError (`bad-directive`)."""
    return scale_params(name)


def parse_exact_scale(name):
    parts = _split_params(name, 1, 2)
    factor = parse_exact_factor(parts[1])
    offset = parse_exact_offset(parts[2]) if len(parts) == 3 else 0
    return factor, offset


def _float_scaled(v, factor, fraction, offset):
    """`v * factor + offset` (or `v * num / den + offset`) in doubles."""
    if fraction is not None:
        return v * fraction[0] / fraction[1] + offset
    return v * factor + offset


def _float_unscaled(x, factor, fraction, offset):
    """`(x - offset) / factor` (or `(x - offset) * den / num`) in doubles."""
    if fraction is not None:
        return (x - offset) * fraction[1] / fraction[0]
    return (x - offset) / factor


def _model_int(name, value):
    """A model integer for an integer-yielding transform; an integral float is
    taken as its integer."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s needs an integer, got %s" % (name, type(value).__name__))
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("%s got non-integral %r" % (name, value))
        value = int(value)
    return value


def _check_int64(name, r):
    if r < _INT64_MIN or r > _INT64_MAX:
        raise ValueError("%s: result %d is outside the signed 64-bit range" % (name, r))
    return r


class IntScaleT(BitsTransform):
    """`uiscale` / `siscale`: exact rational arithmetic, yields int."""

    yields = "int"

    def __init__(self, name, length, signed, factor, offset):
        super().__init__(name, length)
        self.signed = signed
        self.factor = factor
        self.offset = offset
        if signed:
            self.lo, self.hi = -(1 << (length - 1)), (1 << (length - 1)) - 1
        else:
            self.lo, self.hi = 0, (1 << length) - 1

    def decode(self, bits):
        v = self._twos(bits) if self.signed else bits
        return _check_int64(self.name, round_exact(v * self.factor) + self.offset)

    def encode(self, value):
        x = _model_int(self.name, value)
        r = round_exact((x - self.offset) / self.factor)
        if r < self.lo or r > self.hi:
            raise ValueError(
                "%s: value %d gives %d, outside the %d-bit field's range %d..%d"
                % (self.name, x, r, self.length, self.lo, self.hi)
            )
        return r & self.mask


def _smag_decode(bits, length):
    mag = bits & ((1 << (length - 1)) - 1)
    return -mag if bits >> (length - 1) else mag


def _smag_encode(name, r, length):
    m = abs(r)
    if m >= (1 << (length - 1)):
        raise ValueError("%s: magnitude %d does not fit %d bit(s)" % (name, m, length - 1))
    return (1 << (length - 1)) | m if r < 0 else m


class SmagT(BitsTransform):
    yields = "int"

    def decode(self, bits):
        return _smag_decode(bits, self.length)

    def encode(self, value):
        return _smag_encode(self.name, _model_int(self.name, value), self.length)


class SmscaleT(BitsTransform):
    yields = "float"

    def __init__(self, name, length, factor, fraction, offset):
        super().__init__(name, length)
        self.factor, self.fraction, self.offset = factor, fraction, offset

    def decode(self, bits):
        return _float_scaled(float(_smag_decode(bits, self.length)), self.factor, self.fraction, self.offset)

    def encode(self, value):
        x = _finite_number("smscale", value)
        q = _float_unscaled(x, self.factor, self.fraction, self.offset)
        if not math.isfinite(q):
            raise ValueError("%s: value %r is out of the field's range" % (self.name, value))
        return _smag_encode(self.name, round_half_away(q), self.length)


class FloatScaleT(BitsTransform):
    """`f32scale` / `f64scale`: an IEEE float, then the scale in doubles."""

    yields = "float"

    def __init__(self, name, length, factor, fraction, offset):
        super().__init__(name, length)
        self.factor, self.fraction, self.offset = factor, fraction, offset
        self.fmt = ">f" if length == 32 else ">d"

    def decode(self, bits):
        v = struct.unpack(self.fmt, bits.to_bytes(self.length // 8, "big"))[0]
        r = _float_scaled(v, self.factor, self.fraction, self.offset)
        if not math.isfinite(r):
            raise ValueError("%s: the decoded value %r is not finite" % (self.name, r))
        return r

    def encode(self, value):
        x = _finite_number(self.name.split(":")[0], value)
        q = _float_unscaled(x, self.factor, self.fraction, self.offset)
        if not math.isfinite(q):
            raise ValueError("%s: value %r gives a quotient that is not finite" % (self.name, value))
        if self.length == 32:
            return int.from_bytes(_pack_single(self.name, ">f", q), "big")
        return int.from_bytes(struct.pack(">d", q), "big")


def _icao6_char(c):
    if 1 <= c <= 26:
        return chr(64 + c)
    if c == 32 or 48 <= c <= 57:
        return chr(c)
    return None


def _icao6_code(ch):
    o = ord(ch)
    if 65 <= o <= 90:
        return o - 64
    if o == 32 or 48 <= o <= 57:
        return o
    return None


def _ais6_code(ch):
    o = ord(ch)
    if 64 <= o <= 95:
        return o - 64
    if 32 <= o <= 63:
        return o
    return None


def _ascii7_code(ch):
    o = ord(ch)
    return o if 32 <= o <= 126 else None


# set -> (width, padding code, padding character, decode a code, encode a character)
_CHARSETS = {
    "icao6": (6, 32, " ", _icao6_char, _icao6_code),
    "ais6": (6, 0, "@", lambda c: chr(c + 64) if c < 32 else chr(c), _ais6_code),
    "ascii7": (7, 0, "\x00", lambda c: "\x00" if c == 0 else chr(c) if 32 <= c <= 126 else None, _ascii7_code),
}


class CharsT(BitsTransform):
    """`chars:<set>`: `len / w` characters of `w` bits; character `i` is the
    `w`-bit field at bit `i * w` of the field, in the pack's bit order. The
    raw value is the field's bits as `uint` reads them; `msb` is set from the
    pack's `bit_order` once the pack is read."""

    yields = "string"
    bit_ordered = True

    def __init__(self, name, length, charset):
        super().__init__(name, length)
        self.charset = charset
        self.width, self.pad_code, self.pad_char, self.to_char, self.to_code = _CHARSETS[charset]
        self.count = length // self.width
        self.msb = False

    def _shift(self, i):
        return self.length - (i + 1) * self.width if self.msb else i * self.width

    def decode(self, bits):
        w = self.width
        m = (1 << w) - 1
        chars = []
        for i in range(self.count):
            c = (bits >> self._shift(i)) & m
            ch = self.to_char(c)
            if ch is None:
                raise ValueError("%s: code %d is not a character of the set" % (self.name, c))
            chars.append(ch)
        text = "".join(chars)
        if self.charset == "ascii7":
            body = text.rstrip("\x00")
            if "\x00" in body:
                raise ValueError("%s: a NUL is followed by a character that is not NUL" % self.name)
            return body
        return text.rstrip(self.pad_char + " ")

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("%s needs a str, got %s" % (self.name, type(value).__name__))
        if len(value) > self.count:
            raise ValueError("%s: text of %d character(s) does not fit %d" % (self.name, len(value), self.count))
        out = 0
        for i in range(self.count):
            if i < len(value):
                c = self.to_code(value[i])
                if c is None:
                    raise ValueError("%s: character %r is not in the set" % (self.name, value[i]))
            else:
                c = self.pad_code
            out |= c << self._shift(i)
        return out


class OctalT(BitsTransform):
    yields = "string"

    def __init__(self, name, length):
        super().__init__(name, length)
        self.digits = length // 3

    def decode(self, bits):
        return format(bits, "o").rjust(self.digits, "0")

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("octal needs a str, got %s" % type(value).__name__)
        if len(value) != self.digits or any(c not in "01234567" for c in value):
            raise ValueError("octal: %r is not exactly %d digits 0-7" % (value, self.digits))
        return int(value, 8)


def v15_params(name):
    """The parameters of a transform of version 0.15, checked on its text
    alone (step 7 of ruling 5b of 0.11) -> what the transform is built from.
    Raises ValueError for a parameter that does not parse or an unknown
    character set (`bad-directive`)."""
    fam = name.split(":", 1)[0]
    if fam in ("uiscale", "siscale", "iscale"):
        return parse_exact_scale(name)
    if fam in ("smscale", "f32scale", "f64scale", "scale"):
        return parse_float_scale(name)
    if fam in ("smag", "octal"):
        if name != fam:
            raise ValueError("%s takes no parameter" % fam)
        return ()
    if fam == "chars":
        parts = name.split(":")
        if len(parts) != 2 or parts[1] not in _CHARSETS:
            raise ValueError("chars takes one character set, icao6, ais6 or ascii7")
        return parts[1]
    raise ValueError("%r is not a transform of version 0.15" % name)


def v15_len_allowed(name, length):
    """Whether a bits transform of version 0.15 allows bit length `length`;
    `name` has passed `v15_params`. Checked after the file (ruling 25 of 0.5)."""
    fam = name.split(":", 1)[0]
    if fam in ("uiscale", "siscale"):
        return 1 <= length <= 64
    if fam in ("smag", "smscale"):
        return 2 <= length <= 64
    if fam == "f32scale":
        return length == 32
    if fam == "f64scale":
        return length == 64
    if fam == "chars":
        w = _CHARSETS[name.split(":")[1]][0]
        return w <= length <= 2040 and length % w == 0
    if fam == "octal":
        return 3 <= length <= 63 and length % 3 == 0
    return False


def build_v15_bits(name, length):
    """A bits transform of version 0.15 bound to `length`. Its parameters
    have passed `v15_params`; whether it allows `length` is
    `v15_len_allowed`, checked by the caller after the file."""
    fam = name.split(":", 1)[0]
    params = v15_params(name)
    if fam in ("uiscale", "siscale"):
        factor, offset = params
        return IntScaleT(name, length, fam == "siscale", factor, offset)
    if fam == "smag":
        return SmagT(name, length)
    if fam == "smscale":
        return SmscaleT(name, length, *params)
    if fam in ("f32scale", "f64scale"):
        return FloatScaleT(name, length, *params)
    if fam == "chars":
        return CharsT(name, length, params)
    if fam == "octal":
        return OctalT(name, length)
    raise ValueError("%r is not a bits transform of version 0.15" % name)


class TextScaleT(Transform):
    """`scale:<factor>[:<offset>]` on xml, json and geojson: `float`, then the
    scale in doubles; never rounded (ruling 8)."""

    kind = "number"
    yields = "float"
    number_only = True  # a string member is refused on geojson too (ruling 23)

    def __init__(self, name):
        self.name = name
        self.factor, self.fraction, self.offset = parse_float_scale(name)

    def _scaled(self, v):
        r = _float_scaled(v, self.factor, self.fraction, self.offset)
        if not math.isfinite(r):
            raise ValueError("%s: the decoded value is not finite" % self.name)
        return r

    def decode(self, text):
        """`xml` text: number text, as `float` reads it (ruling 2b of 0.11)."""
        return self._scaled(read_number_text(text))

    def decode_json(self, text):
        """A JSON number, as `float` reads it (ruling 2d of 0.11)."""
        return self._scaled(read_json_float(text))

    def encode(self, value):
        x = _finite_number("scale", value)
        return format_float(_float_unscaled(x, self.factor, self.fraction, self.offset))


def exact_decimal_text(q):
    """The plain decimal text of an exact rational, at most 18 fraction digits.
    Raises ValueError when it does not terminate within them."""
    d = q.denominator
    twos = fives = 0
    while d % 2 == 0:
        d //= 2
        twos += 1
    while d % 5 == 0:
        d //= 5
        fives += 1
    if d != 1:
        raise ValueError("the quotient %s does not terminate" % q)
    k = max(twos, fives)
    if k > _MAX_FRACTION_DIGITS:
        raise ValueError("the quotient %s needs %d fraction digits; at most %d are written"
                         % (q, k, _MAX_FRACTION_DIGITS))
    scaled = abs(q.numerator) * (10 ** k // q.denominator)
    whole, frac = divmod(scaled, 10 ** k)
    text = str(whole)
    if frac:
        text += "." + str(frac).rjust(k, "0").rstrip("0")
    return ("-" if q < 0 else "") + text


class IScaleT(Transform):
    """`iscale:<factor>[:<offset>]` on xml, json and geojson: the number's text
    read as an exact decimal, `round(r * factor) + offset`; encode writes the
    exact quotient `(x - offset) / factor`."""

    kind = "number"
    yields = "int"
    number_only = True
    exact = True  # written as its text, never through a double (ruling 7)

    def __init__(self, name):
        self.name = name
        self.factor, self.offset = parse_exact_scale(name)

    def decode(self, text):
        m = _ISCALE_TEXT.match(text)
        if m is None:
            raise ValueError("%s: %r is not -digits[.digits][e[+-]digits]" % (self.name, text))
        sign, whole, frac, exp = m.groups()
        if exp is not None and len(exp.lstrip("+-").lstrip("0")) > 3:
            raise ValueError("%s: exponent %s is above %d in absolute value" % (self.name, exp, _MAX_ISCALE_EXPONENT))
        e = int(exp) if exp is not None else 0
        if abs(e) > _MAX_ISCALE_EXPONENT:
            raise ValueError("%s: exponent %d is above %d in absolute value" % (self.name, e, _MAX_ISCALE_EXPONENT))
        frac = frac or ""
        n = _digits(whole + frac)
        e -= len(frac)
        r = Fraction(n * 10 ** e) if e >= 0 else Fraction(n, 10 ** -e)
        if sign:
            r = -r
        return _check_int64(self.name, round_exact(r * self.factor) + self.offset)

    def decode_json(self, text):
        """A JSON number: its digits as written, read exactly (ruling 7)."""
        return self.decode(text)

    def encode(self, value):
        x = _model_int(self.name, value)
        return exact_decimal_text((x - self.offset) / self.factor)


def build_v15_text(name):
    """`scale` or `iscale` for a text wire. Raises ValueError (`bad-directive`)."""
    fam = name.split(":", 1)[0]
    if fam == "scale":
        return TextScaleT(name)
    if fam == "iscale":
        return IScaleT(name)
    raise ValueError("%r is not a text transform of version 0.15" % name)
