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
`sscale`, `f32`, `f64` and `ascii`. A transform reports a failure as ValueError;
the engine names it `transform-decode` or `transform-encode`.
"""

import math
import re
import struct
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_INTEGER = re.compile(r"[+-]?\d+\Z")
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[Zz]|([+-])(\d{2}):(\d{2}))\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)
# The most digits an integer read from text may have: Python's limit on
# converting a digit string to `int` (4300 by default), so `1e5000` and a
# 5001-digit string fail alike.
_INT_MAX_DIGITS = getattr(sys, "get_int_max_str_digits", lambda: 4300)() or 4300


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


def canonical_number(raw):
    """Canonical text of a JSON number given its raw text (used for residue)."""
    if _INTEGER.match(raw):
        return format_int(int(raw))
    return format_float(float(raw))


def round_half_away(x):
    """Round to the nearest integer, halves away from zero (matches Rust f64::round
    and so the reference engine)."""
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
        t = text.strip()
        if not _DECIMAL.match(t):
            raise ValueError("not a decimal number: %r" % text)
        v = float(t)
        if not math.isfinite(v):
            raise ValueError("out of range: %r" % text)
        return v

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("float transform needs a number, got %s" % type(value).__name__)
        return format_float(value)


class IntT(Transform):
    name = "int"
    kind = "number"
    yields = "int"

    def decode(self, text):
        t = text.strip()
        if _INTEGER.match(t):
            return int(t)
        # Tolerate an integral decimal such as "5.0" or "1e3".
        if _DECIMAL.match(t):
            d = Decimal(t)
            # An exponent makes short text a huge integer (`2e47686170` has 47
            # million digits): building it takes time and memory without bound
            # (0.8, "Malformed input"). An integer of more digits than a digit
            # string may hold here is out of range, as `int(t)` above finds it.
            if d and d.adjusted() >= _INT_MAX_DIGITS:
                raise ValueError("out of range: %r" % text[:40])
            if d == d.to_integral_value():
                return int(d)
        raise ValueError("not an integer: %r" % text)

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
        m = _ISO.match(text.strip())
        if not m:
            raise ValueError("not ISO 8601 YYYY-MM-DDTHH:MM:SS[.fff] with Z or +hh:mm / -hh:mm: %r" % text)
        y, mo, d, h, mi, s, frac, sign, oh, om = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError("invalid date/time %r: %s" % (text, e))
        ms = int((frac or "").ljust(3, "0")[:3])
        offset_ms = 0
        if sign is not None:
            if int(oh) > 23 or int(om) > 59:
                raise ValueError("invalid UTC offset in %r" % text)
            offset_ms = (int(oh) * 60 + int(om)) * 60000 * (-1 if sign == "-" else 1)
        return (dt - _EPOCH) // _MS + ms - offset_ms

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ValueError("iso8601_ms needs integer epoch ms, got %r" % (value,))
        try:
            dt = _EPOCH + timedelta(milliseconds=value)
        except OverflowError:
            raise ValueError("epoch ms out of range: %r" % value)
        if dt.year < 1 or dt.year > 9999:
            raise ValueError("epoch ms out of range: %r" % value)
        return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond // 1000,
        )


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


TRANSFORMS = {t.name: t for t in (StringT(), FloatT(), IntT(), Iso8601MsT())}
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
        return value & self.mask


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
        return self._twos(bits) / self.maxpos * self.range

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("linscale transform needs a number, got %s" % type(value).__name__)
        if not math.isfinite(float(value)):
            raise ValueError("linscale transform got non-finite %r" % value)
        return round_half_away(float(value) / self.range * self.maxpos) & self.mask


_DEC_INT = re.compile(r"[+-]?\d+\Z")


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


def is_bits_transform_name(name):
    return name in ("uint", "twos", "f32", "f64", "ascii") or name.startswith(("linscale:", "uscale:", "sscale:"))


def bits_len_allowed(name, length):
    """Whether a bits transform allows bit length `length` (spec 0.5: `f32` 32,
    `f64` 64, `ascii` a multiple of 8 from 8 to 2040; the rest 1..=64)."""
    if name == "f32":
        return length == 32
    if name == "f64":
        return length == 64
    if name == "ascii":
        return 8 <= length <= 2040 and length % 8 == 0
    return 1 <= length <= 64


def _build_scale(name, length):
    parts = name.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("%s takes the form %s:<factor>[:<offset>]" % (parts[0], parts[0]))
    factor = fraction = None
    if "/" in parts[1]:
        num, _, den = parts[1].partition("/")
        if not _DEC_INT.match(num) or not _DEC_INT.match(den):
            raise ValueError("a fraction factor is <num>/<den> of two decimal integers")
        fraction = (float(int(num)), float(int(den)))
        if fraction[0] == 0 or fraction[1] == 0:
            raise ValueError("a fraction factor's num and den must be non-zero")
    else:
        if not _DECIMAL.match(parts[1]):
            raise ValueError("factor must be a decimal number or <num>/<den>")
        factor = float(parts[1])
        if factor == 0 or not math.isfinite(factor):
            raise ValueError("factor must be finite and non-zero")
    offset = 0.0
    if len(parts) == 3:
        if not _DECIMAL.match(parts[2]):
            raise ValueError("offset must be a decimal number")
        offset = float(parts[2])
        if not math.isfinite(offset):
            raise ValueError("offset must be finite")
    return ScaleT(name, length, parts[0] == "sscale", factor, fraction, offset)


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
    if name.startswith(("uscale:", "sscale:")):
        return _build_scale(name, length)
    if name.startswith("linscale:"):
        parts = name.split(":")
        if len(parts) != 3:
            raise ValueError("linscale takes the form linscale:<maxpos>:<range>")
        try:
            maxpos = int(parts[1])
            value_range = float(parts[2])
        except ValueError:
            raise ValueError("linscale maxpos and range must be numbers")
        if maxpos <= 0:
            raise ValueError("linscale maxpos must be positive")
        if value_range == 0:
            raise ValueError("linscale range must be non-zero")
        return LinscaleT(name, length, maxpos, value_range)
    raise ValueError("unknown bits transform %r" % name)


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
    """Read `text` as a model type (`string`, `int`, `float`). Raises ValueError."""
    if type_name == "string":
        return text
    if type_name == "int":
        if not _INTEGER.match(text):
            raise ValueError("not an integer: %r" % text)
        return int(text)
    if not _DECIMAL.match(text):
        raise ValueError("not a decimal number: %r" % text)
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("out of range: %r" % text)
    return v


def canonical_text(value, type_name):
    """Canonical text of a model value: a string as-is, an integer as decimal
    digits, a float as in ruling 3 of 0.1.1."""
    if type_name == "string":
        return value
    if type_name == "int":
        return format_int(value)
    return format_float(value)


def table_side_type(values):
    """The type of one side of a value table: `int` when every value is an
    integer, `float` when every one is a number, otherwise `string`."""
    if values and all(_INTEGER.match(v) for v in values):
        return "int"
    if values and all(_DECIMAL.match(v) for v in values):
        return "float"
    return "string"


def parse_factor(text):
    """A scale factor as in ruling 1 of 0.5 -> (factor, None) or (None, (num, den)).
    Raises ValueError."""
    if "/" in text:
        num, _, den = text.partition("/")
        if not _DEC_INT.match(num) or not _DEC_INT.match(den):
            raise ValueError("a fraction factor is <num>/<den> of two decimal integers")
        if int(den) == 0:
            raise ValueError("a fraction factor's den must be non-zero")
        return None, (float(int(num)), float(int(den)))
    if not _DECIMAL.match(text):
        raise ValueError("factor must be a decimal number or <num>/<den>")
    factor = float(text)
    if not math.isfinite(factor):
        raise ValueError("factor must be finite")
    return factor, None
