"""The `text` wire (spec/engine-spec.md, Version 0.10): one record of ASCII
text, fields separated by a delimiter, optionally framed by a start text and
an `nmea` checksum. A field may also be read as character columns.

A decoded record is the list of its fields' texts. Inside a group an item is
the slice of the record's fields that belongs to it, so item-relative
locators index it directly.

Locators, parsed by `parse_locator`:

- `("f", i)`              field `i`, its whole text
- `("c", i, from, len)`   a column of field `i`
- `("p", i, j)`           a pair: fields `i` and `j` as one value
"""

import math
import re
from datetime import date

from .errors import BadDirective, MalformedWire, TransformEncodeFailure
from .transforms import Iso8601MsT, StringT, Transform, format_float, format_int, round_half_away

_HEX = "0123456789abcdefABCDEF"
_DIGITS = re.compile(r"[0-9]+\Z")
_INT = re.compile(r"[+-]?[0-9]+\Z")
_DEC = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)\Z")
_HMS = re.compile(r"([0-9]{2})([0-9]{2})([0-9]{2})(?:\.([0-9]+))?\Z")
_DMY = re.compile(r"([0-9]{2})([0-9]{2})([0-9]{2})\Z")
_LIMIT = float(1 << 53)
_I64_MIN, _I64_MAX = -(1 << 63), (1 << 63) - 1
_DAY_MS = 86400000
_EPOCH_ORD = date(1970, 1, 1).toordinal()
_MS_1980 = (date(1980, 1, 1).toordinal() - _EPOCH_ORD) * _DAY_MS
_MS_2080 = (date(2080, 1, 1).toordinal() - _EPOCH_ORD) * _DAY_MS


# --- locators -----------------------------------------------------------------


def _index(text):
    if not (text.isascii() and _DIGITS.match(text)):
        raise ValueError("a text locator index is decimal digits, got %r" % text)
    return int(text)


def parse_locator(locator):
    """-> a parsed text locator. Raises ValueError on bad syntax."""
    if "+" in locator:
        a, _, b = locator.partition("+")
        return ("p", _index(a), _index(b))
    if ":" in locator:
        parts = locator.split(":")
        if len(parts) != 3:
            raise ValueError("a column is <i>:<from>:<len>")
        i, frm, ln = (_index(p) for p in parts)
        if ln < 1:
            raise ValueError("a column's len is at least 1")
        return ("c", i, frm, ln)
    return ("f", _index(locator))


def indexes(loc):
    """The field indexes a parsed locator addresses."""
    if loc[0] == "p":
        return (loc[1], loc[2])
    return (loc[1],)


# --- transforms ---------------------------------------------------------------


def _number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s needs a number, got %s" % (name, type(value).__name__))
    try:
        x = float(value)
    except OverflowError:
        raise ValueError("%s: %r is not a finite double" % (name, value))
    if not math.isfinite(x):
        raise ValueError("%s got non-finite %r" % (name, value))
    return x


def _integer(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s needs an integer, got %s" % (name, type(value).__name__))
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("%s got non-integral %r" % (name, value))
        value = int(value)
    return value


def _rounded(name, q):
    """`round` half away from zero of a double; |n| of 2^53 or more, or a value
    that is not finite, fails (rule 2)."""
    if not math.isfinite(q) or abs(q) >= _LIMIT:
        raise ValueError("%s: %r is out of range" % (name, q))
    n = round_half_away(q)
    if abs(n) >= (1 << 53):
        raise ValueError("%s: %r rounds out of range" % (name, q))
    return n


def read_int(text):
    if not _INT.match(text):
        raise ValueError("not an integer: %r" % text)
    digits = text.lstrip("+-").lstrip("0")
    if len(digits) > 19:
        raise ValueError("integer out of range: %r" % text)
    n = int(text)
    if n < _I64_MIN or n > _I64_MAX:
        raise ValueError("integer out of range: %r" % text)
    return n


def read_float(text):
    if not _DEC.match(text):
        raise ValueError("not a decimal number: %r" % text)
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("out of range: %r" % text)
    return v


class TextIntT(Transform):
    """`int` and `int:<w>` on `text`."""

    kind = "number"
    yields = "int"
    numeric = True
    pair = False

    def __init__(self, name, width=0):
        self.name = name
        self.width = width

    def decode(self, text):
        return read_int(text)

    def encode(self, value):
        n = _integer(self.name, value)
        digits = format_int(abs(n)).rjust(self.width, "0")
        return "-" + digits if n < 0 else digits


class TextFloatT(Transform):
    """`float` on `text`."""

    name = "float"
    kind = "number"
    yields = "float"
    numeric = True
    pair = False

    def decode(self, text):
        return read_float(text)

    def encode(self, value):
        return format_float(_number("float", value))


class DecT(Transform):
    """`dec:<f>` and `dec:<f>:<w>`."""

    kind = "number"
    yields = "float"
    numeric = True
    pair = False

    def __init__(self, name, f, width=0):
        self.name = name
        self.f = f
        self.width = width
        self.scale = float(10 ** f)

    def decode(self, text):
        return read_float(text)

    def encode(self, value):
        n = _rounded(self.name, _number(self.name, value) * self.scale)
        digits = str(abs(n)).rjust(self.f + 1, "0")
        if self.f:
            digits = digits[:-self.f] + "." + digits[-self.f:]
        digits = digits.rjust(self.width, "0")
        return "-" + digits if n < 0 else digits


class DmT(Transform):
    """`dm:<d>:<f>:<PN>`: degrees and minutes in one field, a letter in the other."""

    kind = "string"
    yields = "float"
    numeric = False
    pair = True

    def __init__(self, name, d, f, pos, neg):
        self.name = name
        self.d = d
        self.f = f
        self.pos = pos
        self.neg = neg
        self.scale = float(10 ** f)
        self.unit = 60 * 10 ** f
        self.shape = re.compile(r"([0-9]{%d})([0-9]{2})((?:\.[0-9]+)?)\Z" % d)

    def decode(self, parts):
        text, letter = parts
        m = self.shape.match(text)
        if m is None:
            raise ValueError("%s: %r is not %d digits of degrees and minutes" % (self.name, text, self.d))
        if int(m.group(2)) >= 60:
            raise ValueError("%s: minutes of 60 or more in %r" % (self.name, text))
        if letter != self.pos and letter != self.neg:
            raise ValueError("%s: the letter is %s or %s, got %r" % (self.name, self.pos, self.neg, letter))
        value = int(m.group(1)) + float(m.group(2) + m.group(3)) / 60
        return -value if letter == self.neg else value

    def encode(self, value):
        x = _number(self.name, value)
        m = _rounded(self.name, abs(x) * 60 * self.scale)
        deg, mins = divmod(m, self.unit)
        if deg >= 10 ** self.d:
            raise ValueError("%s: %d degrees do not fit %d digit(s)" % (self.name, deg, self.d))
        whole, frac = divmod(mins, 10 ** self.f)
        text = "%0*d%02d" % (self.d, deg, whole)
        if self.f:
            text += "." + str(frac).rjust(self.f, "0")
        return (text, self.neg if x < 0 and m > 0 else self.pos)


def _hms_text(n, f):
    """`hhmmss[.f digits]` of `n` units of 10^-f seconds since midnight."""
    secs, frac = divmod(n, 10 ** f)
    h, rest = divmod(secs, 3600)
    mi, s = divmod(rest, 60)
    text = "%02d%02d%02d" % (h, mi, s)
    if f:
        text += "." + str(frac).rjust(f, "0")
    return text


def _read_hms(name, text):
    """-> (h * 3600 + m * 60, seconds text, fraction digits or '')."""
    m = _HMS.match(text)
    if m is None:
        raise ValueError("%s: %r is not hhmmss[.s]" % (name, text))
    h, mi, s, frac = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""
    if h >= 24 or mi >= 60 or s >= 60:
        raise ValueError("%s: %r is not a time of day" % (name, text))
    return h * 3600 + mi * 60, s, frac


class HmsT(Transform):
    """`hms:<f>`: seconds since midnight."""

    kind = "string"
    yields = "float"
    numeric = False
    pair = False

    def __init__(self, name, f):
        self.name = name
        self.f = f
        self.scale = float(10 ** f)

    def decode(self, text):
        base, s, frac = _read_hms(self.name, text)
        return base + float("%02d.%s" % (s, frac) if frac else "%d" % s)

    def encode(self, value):
        n = _rounded(self.name, _number(self.name, value) * self.scale)
        if n < 0 or n >= 86400 * 10 ** self.f:
            raise ValueError("%s: %r is not a time of day" % (self.name, value))
        return _hms_text(n, self.f)


class HmsDmyT(Transform):
    """`hms_dmy:<f>`: a time and a date `ddmmyy` as epoch milliseconds UTC."""

    kind = "string"
    yields = "int"
    numeric = False
    pair = True

    def __init__(self, name, f):
        self.name = name
        self.f = f

    def decode(self, parts):
        time_text, date_text = parts
        base, s, frac = _read_hms(self.name, time_text)
        m = _DMY.match(date_text)
        if m is None:
            raise ValueError("%s: %r is not ddmmyy" % (self.name, date_text))
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            day = date(1900 + yy if yy >= 80 else 2000 + yy, mo, d)
        except ValueError:
            raise ValueError("%s: %r is not a date" % (self.name, date_text))
        ms = int(frac[:3].ljust(3, "0"))
        return (day.toordinal() - _EPOCH_ORD) * _DAY_MS + (base + s) * 1000 + ms

    def encode(self, value):
        ms = _integer(self.name, value)
        if ms < _MS_1980 or ms >= _MS_2080:
            raise ValueError("%s: %r is outside the years 1980 to 2079" % (self.name, value))
        days, rest = divmod(ms, _DAY_MS)
        secs, milli = divmod(rest, 1000)
        n = secs * 10 ** self.f + milli // 10 ** (3 - self.f)
        day = date.fromordinal(_EPOCH_ORD + days)
        return (_hms_text(n, self.f), "%02d%02d%02d" % (day.day, day.month, day.year % 100))


class _TextString(StringT):
    numeric = False
    pair = False


class _TextIso(Iso8601MsT):
    numeric = False
    pair = False


_BASE = {"string": _TextString(), "int": TextIntT("int"), "float": TextFloatT(), "iso8601_ms": _TextIso()}
_FAMILIES = ("int", "dec", "dm", "hms", "hms_dmy")


def is_text_only_name(name):
    """A transform valid only on the `text` wire, well formed or not: `int:…`,
    `dec`, `dec:…`, `dm`, `dm:…`, `hms`, `hms:…`, `hms_dmy`, `hms_dmy:…`."""
    head, sep, _ = name.partition(":")
    if head == "int":
        return bool(sep)
    return head in _FAMILIES


def _param(text, lo, hi):
    if not (text.isascii() and _DIGITS.match(text)):
        raise ValueError
    n = int(text)
    if n < lo or n > hi:
        raise ValueError
    return n


def build_transform(name):
    """-> the transform of `name` on the `text` wire, or None when the name is
    not a transform of this wire. A text-only name with bad parameters raises
    ValueError (`bad-directive`, rule 1)."""
    t = _BASE.get(name)
    if t is not None:
        return t
    if not is_text_only_name(name):
        return None
    parts = name.split(":")
    head, args = parts[0], parts[1:]
    try:
        if head == "int" and len(args) == 1:
            return TextIntT(name, _param(args[0], 1, 20))
        if head == "dec" and len(args) in (1, 2):
            return DecT(name, _param(args[0], 0, 9), _param(args[1], 1, 20) if len(args) == 2 else 0)
        if head == "dm" and len(args) == 3:
            letters = args[2]
            if len(letters) != 2 or letters[0] == letters[1] or not all("A" <= c <= "Z" for c in letters):
                raise ValueError
            return DmT(name, _param(args[0], 1, 3), _param(args[1], 0, 9), letters[0], letters[1])
        if head in ("hms", "hms_dmy") and len(args) == 1:
            f = _param(args[0], 0, 3)
            return HmsT(name, f) if head == "hms" else HmsDmyT(name, f)
    except ValueError:
        pass
    raise ValueError("transform %r: bad or missing parameters" % name)


# --- load checks --------------------------------------------------------------


def check_value(value, loc, delimiter, checksum):
    """-> why a `when` or `const` value is refused, or None."""
    if value == "":
        return "is empty"
    if delimiter is not None and delimiter in value:
        return "holds the delimiter"
    if checksum == "nmea" and "*" in value:
        return "holds '*' under 'checksum nmea'"
    if any(not (" " <= c <= "~") for c in value):
        return "holds a character outside 0x20 to 0x7E"
    if value[0] == " " or value[-1] == " ":
        return "has a space at its start or end"
    if loc[0] == "c" and len(value) > loc[3]:
        return "is longer than its column of %d character(s)" % loc[3]
    return None


def check_pack(delimiter, start, checksum, common, kinds, directive_lines, deferred, source, name):
    """The checks of a `text` pack that need the whole file (ruling 2): the
    directives against one another, the locators against `delimiter none`,
    pairs against their transforms, `when` and `const` values, and the group
    and scope rules. Each failure is `bad-directive`, appended to `deferred`
    as (lineno, error)."""

    def bad(lineno, msg, locator=None):
        deferred.append((lineno, BadDirective("%s, at %s:%d" % (msg, source, lineno), format=name, locator=locator)))

    if start is not None and delimiter is not None and delimiter in start:
        bad(directive_lines["start"], "the 'start' text holds the delimiter", "start")
    if delimiter == "*" and checksum == "nmea":
        bad(directive_lines["delimiter"], "'delimiter \"*\"' with 'checksum nmea'", "delimiter")

    def line(ln, item_n):
        loc = ln.loc
        if delimiter is None and any(k != 0 for k in indexes(loc)):
            bad(ln.lineno, "a locator of a field other than 0 under 'delimiter none'", ln.locator)
        if item_n is not None and any(k >= item_n for k in indexes(loc)):
            bad(ln.lineno, "the locator reaches beyond 'item %d'" % item_n, ln.locator)
        if ln.is_field:
            if item_n is not None and ln.optional:
                bad(ln.lineno, "'optional' inside a group", ln.locator)
            return
        if loc[0] == "p":
            bad(ln.lineno, "a 'when' or 'const' takes a field or a column, not a pair", ln.locator)
            return
        if not ln.present:
            why = check_value(ln.value, loc, delimiter, checksum)
            if why is not None:
                bad(ln.lineno, "the value %r %s" % (ln.value, why), ln.locator)

    def body(g):
        if delimiter is None:
            bad(g.lineno, "a 'group' under 'delimiter none'", g.locator)
        for ln in g.lines:
            if ln.is_group:
                bad(ln.lineno, "a group inside a group", ln.locator)
                body(ln)
            else:
                line(ln, g.item_bits)

    def walk(lines):
        for ln in lines:
            if ln.is_group:
                body(ln)
            else:
                line(ln, None)

    def scope(lines):
        groups = [ln for ln in lines if ln.is_group]
        if not groups:
            return
        for g in groups[1:]:
            bad(g.lineno, "a second group in one scope", g.locator)
        first = groups[0].loc[0]
        for ln in lines:
            if not ln.is_group and max(indexes(ln.loc)) >= first:
                bad(ln.lineno, "a line at or beyond the start of group %s" % groups[0].list, ln.locator)

    walk(common)
    for k in kinds:
        walk(k.lines)
    scope(common)
    for k in kinds:
        scope(common + k.lines)


# --- decode -------------------------------------------------------------------


def parse(pack, data):
    """Steps 1 to 5 of "Reading a record" -> the list of field texts."""
    fmt = pack.name
    if data.endswith("\r\n"):
        data = data[:-2]
    elif data.endswith("\n"):
        data = data[:-1]
    for c in data:
        if not (" " <= c <= "~"):
            raise MalformedWire("the record holds a character outside 0x20 to 0x7E: %r" % c, format=fmt)
    start = pack.start
    if start:
        if not data.startswith(start):
            raise MalformedWire("the record does not begin with %r" % start, format=fmt)
        data = data[len(start):]
    if pack.checksum == "nmea":
        if len(data) < 3 or data[-3] != "*" or data[-2] not in _HEX or data[-1] not in _HEX:
            raise MalformedWire("the record does not end with '*' and two hexadecimal digits", format=fmt)
        want = int(data[-2:], 16)
        data = data[:-3]
        have = 0
        for c in data:
            have ^= ord(c)
        if have != want:
            raise MalformedWire("checksum %02X, the record's XOR is %02X" % (want, have), format=fmt)
        if "*" in data:
            raise MalformedWire("a '*' in the body of the record", format=fmt)
    if pack.delimiter is None:
        return [data]
    return data.split(pack.delimiter)


def _whole(fields, i):
    if i < len(fields):
        text = fields[i]
        if text:
            return text
    return None


def _column(fields, loc, numeric):
    if loc[1] >= len(fields):
        return None
    text = fields[loc[1]]
    frm = loc[2]
    if len(text) <= frm:
        return None
    text = text[frm:frm + loc[3]].rstrip(" ")
    if numeric:
        text = text.lstrip(" ")
    return text or None


def get(pack, fields, f):
    """The raw value of one `field` line: its text, a pair of texts, or None
    when absent."""
    loc = f.loc
    kind = loc[0]
    if kind == "f":
        return _whole(fields, loc[1])
    if kind == "c":
        return _column(fields, loc, f.transform.numeric)
    a = _whole(fields, loc[1])
    b = _whole(fields, loc[2])
    if a is None or b is None:
        return None
    return (a, b)


def when_holds(pack, fields, when):
    loc = when.loc
    if loc[0] == "c":
        text = _column(fields, loc, False)
    else:
        text = _whole(fields, loc[1])
    if when.present:
        return text is not None
    return text is not None and text == when.value


def items(pack, fields, group):
    """-> the items of a group (slices of the record), or None when the record
    has no field at the group's index."""
    i = group.loc[0]
    n = group.item_bits
    if len(fields) <= i:
        return None
    return [fields[s:s + n] for s in range(i, len(fields), n)]


def _facts(plan):
    """-> (addressed, group start or None, highest addressed index, the
    indexes whose every addressing line is `optional`) of a scope. A line
    addresses a field as the Residue section states."""
    facts = plan.cache.get("text")
    if facts is not None:
        return facts
    addressed = set()
    not_optional = set()
    start = None
    for ln in plan.lines:
        if ln.is_group:
            if start is None:
                start = ln.loc[0]
            continue
        if not ln.is_field and ln.present:
            continue
        for k in indexes(ln.loc):
            addressed.add(k)
            if not (ln.is_field and ln.optional):
                not_optional.add(k)
    highest = max(addressed) if addressed else -1
    optional_only = addressed - not_optional
    facts = plan.cache["text"] = (addressed, start, highest, optional_only)
    return facts


def residue(pack, fields, plan):
    """-> {index: text} of the non-empty fields no line of the scope addresses."""
    addressed, start, _, _ = _facts(plan)
    out = {}
    for k, text in enumerate(fields):
        if not text or k in addressed or (start is not None and k >= start):
            continue
        out[str(k)] = text
    return out


# --- encode -------------------------------------------------------------------


def _checked(pack, f, text):
    delim = pack.delimiter
    for c in text:
        if not (" " <= c <= "~"):
            raise TransformEncodeFailure(
                "the text holds a character outside 0x20 to 0x7E: %r" % c, format=pack.name, locator=f.locator
            )
    if delim is not None and delim in text:
        raise TransformEncodeFailure("the text %r holds the delimiter" % text, format=pack.name, locator=f.locator)
    if pack.checksum == "nmea" and "*" in text:
        raise TransformEncodeFailure("the text %r holds '*'" % text, format=pack.name, locator=f.locator)
    return text


def _pad(pack, f, text, width, numeric):
    if len(text) > width:
        raise TransformEncodeFailure(
            "the text %r is longer than its column of %d" % (text, width), format=pack.name, locator=f.locator
        )
    if numeric:
        if text.startswith("-"):
            return "-" + text[1:].rjust(width - 1, "0")
        return text.rjust(width, "0")
    return text.ljust(width, " ")


def _compose(pack, enc, base, out):
    """Write one encoded scope into `out` (field index -> text), its locators
    counted from field `base`."""
    plan = enc.plan
    values = enc.values
    whole = {}
    cols = {}
    over = {}
    for li, ln in enumerate(plan.lines):
        if ln.is_group:
            continue
        loc = ln.loc
        if ln.is_field:
            if li not in values:
                continue
            v = values[li]
            if loc[0] == "p":
                whole.setdefault(base + loc[1], _checked(pack, ln, v[0]))
                whole.setdefault(base + loc[2], _checked(pack, ln, v[1]))
            elif loc[0] == "f":
                whole.setdefault(base + loc[1], _checked(pack, ln, v))
            else:
                text = _pad(pack, ln, _checked(pack, ln, v), loc[3], ln.transform.numeric)
                cols.setdefault(base + loc[1], []).append((loc[2], text))
        elif not ln.present:
            over.setdefault(base + loc[1], []).append(ln)
    for k in set(whole) | set(cols) | set(over):
        if k in whole:
            chars = list(whole[k])
        else:
            chars = []
            written = set()
            for frm, text in cols.get(k, ()):
                if len(chars) < frm + len(text):
                    chars.extend(" " * (frm + len(text) - len(chars)))
                for p, c in enumerate(text, frm):
                    if p not in written:
                        written.add(p)
                        chars[p] = c
        locked = set()
        for ln in over.get(k, ()):
            loc = ln.loc
            if loc[0] == "f":
                text = ln.value
                if not locked:
                    chars = list(text)
                else:
                    end = max(len(text), max(locked) + 1)
                    chars.extend(" " * (end - len(chars)))
                    chars = [chars[p] if p in locked or p >= len(text) else text[p] for p in range(end)]
                locked.update(range(len(text)))
            else:
                frm = loc[2]
                text = ln.value.ljust(loc[3], " ")
                if len(chars) < frm + len(text):
                    chars.extend(" " * (frm + len(text) - len(chars)))
                for p, c in enumerate(text, frm):
                    if p not in locked:
                        locked.add(p)
                        chars[p] = c
        out[k] = "".join(chars)


def write(pack, enc, residue):
    """Steps 1 to 6 of "Writing a record"."""
    plan = enc.plan
    addressed, start, highest, optional_only = _facts(plan)
    out = {}
    _compose(pack, enc, 0, out)
    last = highest
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if not children:
            continue
        first, n = g.loc[0], g.item_bits
        for c, child in enumerate(children):
            _compose(pack, child, first + c * n, out)
        last = max(last, first + len(children) * n - 1)
    group_fields = set(range(highest + 1, last + 1)) if last > highest else set()
    for key, text in residue.items():
        if not (key.isascii() and key.isdigit()):
            continue
        k = int(key)
        if k in addressed or (start is not None and k >= start) or not isinstance(text, str):
            continue
        out[k] = _checked(pack, _ResidueLine(key), text)
        last = max(last, k)
    count = last + 1
    while count > 0:
        k = count - 1
        if out.get(k) or k not in optional_only or k in group_fields:
            break
        count -= 1
    record = (pack.delimiter or "").join([out.get(k, "") for k in range(count)])
    if pack.checksum == "nmea":
        x = 0
        for c in record:
            x ^= ord(c)
        record += "*%02X" % x
    return (pack.start or "") + record


class _ResidueLine:
    __slots__ = ("locator",)

    def __init__(self, locator):
        self.locator = locator
