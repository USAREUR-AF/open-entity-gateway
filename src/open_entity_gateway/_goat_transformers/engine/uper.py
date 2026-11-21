"""ASN.1 unaligned PER in a sequential `bits` pack (spec/engine-spec.md,
"Version 0.19: ASN.1 unaligned PER"): the UPER locators, their transforms,
and the number syntax of rulings 15 of 0.19.

A UPER locator hands its transform the value X.691 encodes, not raw bits:
the integer for `int:` and `enum:`, the list of 7-bit codes for `chars:`,
the octets for `octets:`.
"""

import re

from .transforms import _build_scale, round_half_away
from .octets import HexT, Utf8T

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
PREFIXES = ("int:", "enum:", "chars:", "octets:")
_SIGNED = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_DIGITS = re.compile(r"(?:0|[1-9][0-9]*)\Z")


def parse_number(text, signed=False):
    """Ruling 15 of 0.19: an optional `-` (locators only) and digits with no
    leading zero; `-0` is refused. -> int, or None."""
    if not text.isascii() or (_SIGNED if signed else _DIGITS).match(text) is None or text == "-0":
        return None
    return int(text)


class Loc:
    """A parsed UPER locator. `form` is int | enum | chars | octets; `lb` and
    `ub` the constraint (`ub` None for no upper bound, `lb` None for `int::`),
    `ext` the extension marker; for `enum`, `lb` 0 and `ub` n - 1."""

    __slots__ = ("form", "lb", "ub", "ext", "n")

    def __init__(self, form, lb, ub, ext=False, n=None):
        self.form = form
        self.lb = lb
        self.ub = ub
        self.ext = ext
        self.n = n


def parse_locator(text):
    """-> Loc. Raises ValueError for anything the table of locators does not allow."""
    parts = text.split(":")
    form = parts[0]
    bad = ValueError("bad UPER locator %r" % text)
    if form == "int":
        if len(parts) == 4:
            if parts[3] != "ext":
                raise bad
            ext = True
        elif len(parts) == 3:
            ext = False
        else:
            raise bad
        a, b = parts[1], parts[2]
        if a == "" and b == "" and not ext:
            return Loc("int", None, None)
        lb = parse_number(a, True)
        if lb is None or not INT64_MIN <= lb <= INT64_MAX:
            raise bad
        if b == "" and not ext:
            return Loc("int", lb, None)
        ub = parse_number(b, True)
        if ub is None or not lb <= ub <= INT64_MAX:
            raise bad
        return Loc("int", lb, ub, ext)
    if form == "enum":
        if len(parts) == 3 and parts[2] == "ext":
            ext = True
        elif len(parts) == 2:
            ext = False
        else:
            raise bad
        n = parse_number(parts[1], True)
        if n is None or not 1 <= n <= 65536:
            raise bad
        return Loc("enum", 0, n - 1, ext, n)
    if form in ("chars", "octets"):
        if len(parts) == 2 and parts[1] == "":
            return Loc(form, 0, None)
        if len(parts) != 3:
            raise bad
        lb = parse_number(parts[1], True)
        ub = parse_number(parts[2], True)
        if lb is None or ub is None or not 0 <= lb <= ub < 65536:
            raise bad
        return Loc(form, lb, ub)
    raise bad


def takes(form, tname):
    """Whether the locator form takes the transform (Transforms on UPER locators)."""
    if form == "int":
        return tname in ("uint", "twos") or tname.startswith(("uscale:", "sscale:"))
    if form == "enum":
        return tname == "uint"
    if form == "chars":
        return tname == "ascii"
    return tname in ("hex", "utf8")


class IntT:
    """`uint`, `twos`, `uscale:`, `sscale:` on an `int:` or `enum:` locator:
    the integer value `v`. `uint` and `twos` give `v` (ruling 4 of 0.19); the
    scales give `v * f + o` in doubles (ruling 1 of 0.5), and encode
    `round((x - o) / f)`, halves away from zero. The constraint is checked by
    the writer."""

    kind = "uper"

    def __init__(self, name):
        self.name = name
        self.scale = None
        if name.startswith(("uscale:", "sscale:")):
            self.scale = _build_scale(name, 64)  # ValueError for parameters that do not parse
            self.yields = "float"
        else:
            self.yields = "int"

    def decode(self, v):
        s = self.scale
        if s is None:
            return v
        if s.fraction is not None:
            scaled = float(v) * s.fraction[0] / s.fraction[1]
        else:
            scaled = float(v) * s.factor
        return scaled + s.offset

    def encode(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s needs a number, got %s" % (self.name, type(value).__name__))
        s = self.scale
        if s is None:
            if isinstance(value, float):
                if not value.is_integer():
                    raise ValueError("%s got non-integral %r" % (self.name, value))
                value = int(value)
            return value
        x = float(value)
        if s.fraction is not None:
            q = (x - s.offset) * s.fraction[1] / s.fraction[0]
        else:
            q = (x - s.offset) / s.factor
        if q != q or q in (float("inf"), float("-inf")):
            raise ValueError("%s: value %r is not a finite number" % (self.name, value))
        return round_half_away(q)


class CharsT:
    """`ascii` on a `chars:` locator: each character a code from 0 to 127. No
    NUL is removed and no printable check is made (0.19)."""

    kind = "uper"
    name = "ascii"
    yields = "string"

    def decode(self, codes):
        return "".join(map(chr, codes))

    def encode(self, value):
        if not isinstance(value, str):
            raise ValueError("ascii transform needs a str, got %s" % type(value).__name__)
        codes = [ord(c) for c in value]
        for c in codes:
            if c > 127:
                raise ValueError("ascii: character U+%04X is above 127" % c)
        return codes


def build_transform(form, tname):
    """-> the transform of a UPER locator; the caller has checked `takes`."""
    if form == "chars":
        return CharsT()
    if form == "octets":
        return HexT() if tname == "hex" else Utf8T()
    return IntT(tname)


def twos_octets(v):
    """The fewest octets of `v` as a two's-complement number."""
    return ((v if v >= 0 else ~v).bit_length() + 8) // 8


def uint_octets(u):
    """The fewest octets of `u` as an unsigned number, at least one."""
    return max(1, (u.bit_length() + 7) // 8)
