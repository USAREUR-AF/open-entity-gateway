"""pack.def parsing (grammar: spec/engine-spec.md, "Pack definition grammar")."""

import os
import re
from dataclasses import dataclass, field as dc_field

from .errors import (
    BadDirective, BadLocator, DuplicateKind, MissingDistribution, MissingFormat, MissingRoot, MissingSource,
    MissingVersion, MissingWire, UnknownDirective, UnknownTransform, UnknownWire, UnterminatedQuote,
)
from .transforms import (
    TRANSFORMS, JSON_TRANSFORMS, build_bits_transform, bits_len_allowed, format_float,
    PROTOBUF_TRANSFORMS, UnknownTransformName, check_transform_text, read_uint_text, transform_valid_on,
    build_v15_text, v15_family, terminator_code,
)
from ._build import WIRES as BUILD_WIRES

# The wire-specific parts of the pack language this module calls, each only
# for a pack on its wire. A build without the wire leaves the module out, and
# such a pack is `unknown-wire` at line check 6 before any of them is reached
# (spec 0.24, ruling 1c).
if "bits" in BUILD_WIRES:
    from . import uper
if "text" in BUILD_WIRES:
    from . import wire_text
if "tlv" in BUILD_WIRES:
    from . import wire_tlv

WIRES = ("xml", "geojson", "json", "bits", "protobuf", "text", "tlv")  # the wires of the specification
BIT_ORDERS = ("lsb", "msb", "msb-lsb")  # `msb-lsb`: 0.22
DISTRIBUTIONS = ("public", "controlled")  # public is below controlled
CORE = "core"
# Ruling 5 of 0.11. The directives of a pack, in the order the table states them.
PACK_DIRECTIVES = frozenset((
    "format", "version", "distribution", "source", "uses", "wire", "root", "field", "when", "const", "kind",
    "requires", "default", "map", "typemap", "group", "end", "word_bytes", "bit_order", "layout", "also", "pad",
    "align", "presence", "if", "opt", "chain", "fx", "length", "delimiter", "start", "checksum",
    "tag", "key",  # the `tlv` wire (0.13)
    "armor",  # the `bits` wire (0.14)
    "element",  # the `xml` wire (0.18)
    # 0.19 (ruling 21): directives of the language on every wire, `bad-directive`
    # outside a UPER pack, checked after the file is read
    "encoding", "ext", "choice", "alt",
    "switch", "case", "else",  # sequential layouts (0.20)
    "count",  # sequential layouts (0.21)
))
# The lines that stand after the `wire` line (5b): before it, `missing-wire`.
NEEDS_WIRE = frozenset((
    "root", "field", "when", "const", "group", "end", "word_bytes", "bit_order", "layout", "also", "pad", "align",
    "presence", "if", "opt", "chain", "fx", "length",
))
# The number of arguments of each directive: (at least, at most or None). 5e
# names the directives of exactly one argument.
_ONE_ARG = ("format", "version", "distribution", "source", "uses", "wire", "root", "kind", "word_bytes",
            "bit_order", "layout", "pad", "align", "delimiter", "start")
ARG_COUNTS = dict({kw: (1, 1) for kw in _ONE_ARG}, **{
    "when": (2, 2), "const": (2, 2), "default": (2, 2), "map": (3, 3), "typemap": (2, 2), "presence": (2, 2),
    "end": (0, 0), "opt": (0, 0), "chain": (0, 0), "fx": (0, 0),
    "field": (3, None), "requires": (1, None), "group": (2, None), "also": (1, None), "if": (1, None),
    "length": (1, None),
    # 0.13 adds `checksum sum16 <tag>` (two arguments) to the one of 5e, and
    # the `tlv` lines `tag` and `key`, whose form is checked after the file.
    "checksum": (1, 2), "tag": (1, None), "key": (1, None),
    # 0.14: `armor <hex | sixbit>`, exactly one argument, checked as the line
    # is read (ruling 13 of 0.14)
    "armor": (1, 1),
    # 0.18: `element <path>`, one path
    "element": (1, 1),
    # 0.19: `encoding <none | uper>`, `ext`, `choice <n> [ext]`, `alt <i>`;
    # their counts are checked as the line is read, as for `armor`
    "encoding": (1, 1), "ext": (0, 0), "choice": (1, 2), "alt": (1, 1),
    # 0.20: `switch <key>`, `case <values>`, `else`
    "switch": (1, 1), "case": (1, 1), "else": (0, 0),
    # 0.21: `count <name> <len> [plus <n>]`; the form of the options is checked after the file
    "count": (2, 4),
})
HEADER_ONCE = ("format", "version", "wire", "distribution")  # a second one is `bad-directive` (5b step 5)
_SINGLE = ("word_bytes", "bit_order", "layout")
LAYOUTS = ("fixed", "sequential")
# Lines of a sequential pack (0.8): each is `bad-directive` in a fixed-layout pack.
_SEQ_ONLY = ("pad", "align", "presence", "opt", "chain", "fx", "length", "also",
             "switch", "case", "else",  # 0.20: `switch` is a block of a sequential layout
             "count")  # 0.21: `count <name> <len> [plus <n>]`
_GROUP_LINES = ("field", "const", "default", "group", "if", "end")
# The lines of a gate block (0.17).
_GATE_LINES = ("field", "const", "default", "if", "end")
FIELD_OPTIONS = ("via", "swap", "trim", "optional", "absent", "with", "stop")  # `stop`: 0.22
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
TEXT_DIRECTIVES = ("delimiter", "start", "checksum")  # the `text` wire (0.10)
ARMORS = ("hex", "sixbit")  # the `bits` wire (0.14)


@dataclass
class Field:
    key: str
    locator: str
    transform: object  # a transforms.Transform
    loc: tuple  # parsed locator, wire-specific
    lineno: int = 0
    via: str = None         # the value table named by `via <table>` (0.6)
    swap: bool = False      # bits: the field's bytes are taken in reverse order
    optional: bool = False  # bits: absent when any of its bits lies beyond the input
    # Set when the pack is checked against the loaded modules (Engine.add):
    key_type: str = None    # the key's declared type
    via_dec: dict = None    # canonical native text -> model value
    via_enc: dict = None    # canonical model text -> native text
    # `absent <value>[,<value>...]` (0.9): the raw wire values that mean "no
    # value", as the wire compares them, and the first as the wire writes it.
    absent: frozenset = None
    absent_out: object = None
    gate: object = None     # bits, fixed layout: the innermost gate block holding the line (0.17)
    with_key: str = None    # `with <other-key>` (0.18): written only when the scope holds that key

    is_field = True
    is_const = False
    is_group = False


@dataclass
class When:
    """`when <locator> <value>`. `present` is the presence test; otherwise
    `value` is what the raw wire value is compared with: an unsigned integer on
    `bits` and `protobuf`, text on the text wires (a leading `=` stripped;
    `forced` records that it was there)."""

    locator: str
    loc: tuple
    value: object = None
    present: bool = False
    forced: bool = False
    lineno: int = 0
    # 0.20: every value of the list. `bits` and `protobuf`: ((lo, hi), ...), a
    # single value as (v, v); text wires: the texts. None: `value` alone.
    ranges: tuple = None
    texts: frozenset = None

    is_field = False
    is_const = False
    is_group = False
    gate = None

    def __post_init__(self):
        if self.ranges is None and isinstance(self.value, int) and not self.present:
            self.ranges = ((self.value, self.value),)
        if self.texts is None and isinstance(self.value, str):
            self.texts = frozenset((self.value,))
        # more than one value: ruling 3 of 0.20 applies on encode
        self.listed = (self.ranges is not None and (len(self.ranges) > 1 or self.ranges[0][0] != self.ranges[0][1]))

    def holds_int(self, v):
        for lo, hi in self.ranges:
            if lo <= v <= hi:
                return True
        return False


@dataclass
class Const:
    """`const <locator> <value>` (0.6): written on encode as a `when` value is,
    not checked on decode; the member it names is mapped."""

    locator: str
    loc: tuple
    value: object = None
    forced: bool = False
    lineno: int = 0
    gate: object = None  # the innermost gate block holding the line (0.17)

    is_field = False
    is_const = True
    is_group = False
    present = False


@dataclass
class Element:
    """`element <path>` (0.18, xml): the element at `path`, and each on the way
    to it, is written on encode even when nothing inside it is. Decode ignores
    it; it maps no member."""

    locator: str
    loc: tuple  # the element names
    lineno: int = 0

    is_field = False
    is_const = False
    is_group = False
    is_element = True
    present = True  # like `when <locator> present`: not in a plan's order nor mapped
    gate = None


class ValueCheck:
    """A list of values read as a key's declared type when the pack is loaded
    (0.20): a `requires <key>=<value>[,...]` term (`target` is the Kind) or a
    `case` line (`target` is the Case). `canon` is set by Engine.add: the
    canonical texts of the values."""

    __slots__ = ("what", "key", "values", "lineno", "target", "canon", "switch", "key_type")

    def __init__(self, what, key, values, lineno, target, switch=None):
        self.what = what
        self.key = key
        self.values = values
        self.lineno = lineno
        self.target = target
        self.switch = switch
        self.canon = None
        self.key_type = None


def requires_term(term, bad):
    """-> (key, values or None) of one `requires` term (0.20 ruling 4): split
    at the first `=`; the rest is the value list."""
    if "=" not in term:
        return term, None
    key, rest = term.split("=", 1)
    if not key:
        raise bad("the 'requires' term %r has no key before '='" % term)
    if not rest:
        raise bad("the 'requires' term %r has no value after '='" % term)
    return key, split_list(rest, bad)


@dataclass
class Default:
    """`default <model-key> <value>` (0.6). `value` is `text` read as the key's
    declared type, set when the pack is checked against the loaded modules."""

    key: str
    text: str
    lineno: int = 0
    value: object = None


@dataclass
class Kind:
    name: str
    lines: list = dc_field(default_factory=list)     # Field, When and Const, in line order
    requires: list = dc_field(default_factory=list)  # model keys and lists
    requires_keys: tuple = ()                        # set by Engine.add: the scalar keys among them
    requires_lists: tuple = ()                       # set by Engine.add: the lists among them
    when_lines: tuple = ()                           # set when the pack is built: the kind's `when` lines
    lineno: int = 0
    root: str = None                                 # xml: the kind's own root element (0.6)
    defaults: list = dc_field(default_factory=list)  # Default lines of the kind
    gates: list = dc_field(default_factory=list)     # the kind's gate blocks, at any depth, in line order (0.17)
    requires_eq: list = dc_field(default_factory=list)  # ValueCheck of each `requires <key>=<values>` term (0.20)

    @property
    def fields(self):
        return [ln for ln in self.lines if ln.is_field]

    @property
    def whens(self):
        return [ln for ln in self.lines if not ln.is_field and not ln.is_const and not ln.is_group
                and not getattr(ln, "is_element", False)]


class Group:
    """`group <list> <locator> ... end` (0.7): the part of a message that
    repeats, mapped to a list of the model. `lines` and `defaults` are the body,
    every locator of which is read relative to one item. On `bits`, `loc` is
    `(word, pos)` of the first item, `item_bits` the nominal size of an item,
    and the count is `count_loc` (a `<word>:<pos>:<len>` of the enclosing
    scope) or the fixed `count_n`."""

    __slots__ = ("list", "locator", "loc", "lineno", "lines", "defaults", "item_bits", "count_loc", "count_n",
                 "_plan", "_seen", "broken", "gates")

    is_field = False
    is_const = False
    is_group = True
    present = False
    gate = None

    def __init__(self, list, locator, loc, lineno=0, item_bits=None, count_loc=None, count_n=None):
        self.list = list
        self.locator = locator
        self.loc = loc
        self.lineno = lineno
        self.lines = []       # Field, Const and Group, in line order
        self.defaults = ()    # Default lines of the body
        self.item_bits = item_bits
        self.count_loc = count_loc
        self.count_n = count_n
        self._plan = None
        self._seen = None     # the lists mapped by the groups of the body, while the file is read
        self.broken = False   # its options failed a check made after the file is read
        self.gates = []       # the gate blocks of the body, at any depth, in line order (0.17)

    @property
    def plan(self):
        """The body as a scope: what applies to one item."""
        plan = self._plan
        if plan is None:
            plan = self._plan = Plan(self.lines, self.defaults, gates=self.gates)
        return plan


class Gate:
    """`if [!]<locator> <value>[,<value>...]` ... `end` on a fixed-layout `bits`
    pack (0.17). The block holds when the unsigned value of the locator's bits
    is one of `values` (with `negate`, none of them) and the enclosing block,
    `parent`, holds. `defaults` maps a key to the Default line of the block.
    `enc_value` is the value encode gives the gate of an open block: the first
    listed value, or with `!` the smallest value not listed (None when the
    list names every value)."""

    __slots__ = ("locator", "loc", "values", "vset", "negate", "parent", "defaults", "lineno", "enc_value")

    is_field = False
    is_const = False
    is_group = False
    present = False
    gate = None

    def __init__(self, locator, loc, values, negate, parent, lineno):
        self.locator = locator
        self.loc = loc
        self.values = values
        self.vset = frozenset(values)
        self.negate = negate
        self.parent = parent
        self.defaults = {}
        self.lineno = lineno
        if not negate:
            self.enc_value = values[0]
        else:
            v = 0
            while v in self.vset:
                v += 1
            self.enc_value = v if v < (1 << loc[2]) else None


def _resolve_gate(name, args, parent, source, lineno):
    """-> Gate for one `if` line of a fixed-layout `bits` pack (0.17). Every
    error is `bad-directive`: a locator that is not `<word>:<pos>:<len>` with
    `len` 1 to 64 (a presence name is a sequential layout's), a missing or
    empty value list, a value that is not an unsigned decimal integer, does
    not fit `len` or is listed twice."""
    where = "%s:%d" % (source, lineno)

    def bad(msg):
        return BadDirective("%s, at %s" % (msg, where), format=name, locator=args[0] if args else None)

    if not args:
        raise bad("'if' takes a locator and a list of values")
    text = args[0]
    negate = text.startswith("!")
    locator = text[1:] if negate else text
    try:
        loc = parse_bits_locator(locator)
    except ValueError as e:
        raise bad("an 'if' of a fixed layout takes a <word>:<pos>:<len> locator (%s)" % e)
    if not 1 <= loc[2] <= 64:
        raise bad("a gate locator is 1 to 64 bits")
    if len(args) < 2:
        raise bad("'if' has an empty value list")
    if len(args) > 2:  # ruling 12 of 0.17: a third token, or a space after a comma
        raise bad("'if' takes one locator and one value list, got %d token(s)" % len(args))
    values = []
    for v in args[1].split(","):
        if not (v.isascii() and v.isdigit()):
            raise bad("a gate value is an unsigned decimal integer, got %r" % v)
        n = int(v)
        if n >= (1 << loc[2]):
            raise bad("gate value %d does not fit %d bit(s)" % (n, loc[2]))
        if n in values:
            raise bad("gate value %d is listed twice" % n)
        values.append(n)
    return Gate(locator, loc, tuple(values), negate, parent, lineno)


class Plan:
    """What applies to one scope: a message of one kind (or of a pack with no
    kinds), or one item of a group. The lines in line order (common first);
    the `when` lines among them; the `field` and `group` lines with their
    index; the defaults in scope; each line's locator once, in line order; and
    the locators that are mapped, so are not residue (every `field`, `const`
    and `group`, every `when` with a value; `when <locator> present` does not
    map its member, 0.5 ruling 27)."""

    __slots__ = ("lines", "whens", "defaults", "order", "mapped", "root", "cache", "fields", "groups",
                 "ifields", "igroups", "gates")

    def __init__(self, lines, defaults=(), root=None, gates=()):
        self.lines = lines
        self.gates = gates  # bits: the gate blocks of the scope, outer before nested, in line order (0.17)
        ifields = self.ifields = []
        igroups = self.igroups = []
        for i, ln in enumerate(lines):
            if ln.is_field:
                ifields.append((i, ln))
            elif ln.is_group:
                igroups.append((i, ln))
        self.fields = [ln for _, ln in ifields]
        self.groups = [ln for _, ln in igroups]
        self.defaults = {d.key: d.value for d in defaults} if defaults else {}
        self.root = root
        self.cache = {}  # per-wire derived facts (bits: the bits a scope needs)

    def __getattr__(self, name):
        # `whens`, `order` and `mapped` are worked out when first asked for: a
        # generated pack has many thousands of scopes and a run touches few.
        if name == "whens":
            value = self.whens = [
                ln for ln in self.lines if not ln.is_field and not ln.is_const and not ln.is_group
                and not getattr(ln, "is_element", False)
            ]
            return value
        if name == "order" or name == "mapped":
            # Built whole, then published (spec 0.25, ruling 1c): a thread
            # never sees a half-built value of another.
            order = []
            mapped = set()
            for ln in self.lines:
                if not ln.is_field and ln.present:
                    continue
                if ln.locator not in mapped:
                    mapped.add(ln.locator)
                    order.append(ln.locator)
            self.mapped = mapped
            self.order = order
            return order if name == "order" else mapped
        raise AttributeError(name)


@dataclass
class Pack:
    name: str
    version: str
    wire: str
    root: str = None
    word_bytes: int = None
    bit_order: str = "lsb"  # bits wire only
    common: list = dc_field(default_factory=list)  # Field, When and Const lines before the first `kind`
    kinds: list = dc_field(default_factory=list)   # Kind, in pack order; empty for a pack with no `kind` line
    # every mapped, required or defaulted key, in line order: (key, Field | Default | None)
    key_refs: list = dc_field(default_factory=list)
    native_to_cot: dict = dc_field(default_factory=dict)
    cot_to_native: dict = dc_field(default_factory=dict)
    path: str = None
    distribution: str = None
    sources: list = dc_field(default_factory=list)
    uses: list = dc_field(default_factory=list)  # modules named in `uses` lines; core is always used
    defaults: list = dc_field(default_factory=list)  # common Default lines
    layout: str = "fixed"  # bits: "sequential" from 0.8
    seq: object = None     # a sequential pack: the seqdef.Body of the message
    tables: dict = dc_field(default_factory=dict)    # value tables: name -> [(native, model)] in line order
    delimiter: str = ","   # text: the field delimiter, None for `delimiter none`
    start: str = None      # text: the start text
    checksum: str = "none" # text: `none` or `nmea`
    tlv_tag: str = "ber-oid"   # tlv: `ber-oid`, `1`, `2` or `4`
    tlv_length: str = "ber"    # tlv: `ber`, `1`, `2` or `4`
    tlv_key: bytes = None      # tlv: the bytes the message begins with
    tlv_checksum: int = None   # tlv: the tag of the `sum16` checksum item
    armor: str = "hex"         # bits: `hex` or `sixbit` (0.14)
    gates: list = dc_field(default_factory=list)     # the common gate blocks, at any depth, in line order (0.17)
    uper: bool = False         # bits, sequential: `encoding uper` (0.19)
    value_checks: list = dc_field(default_factory=list)  # ValueCheck, in line order (0.20)
    _plans: dict = dc_field(default_factory=dict, repr=False)
    _kind_index: dict = dc_field(default_factory=dict, repr=False)
    _kinds_by_root: dict = dc_field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.has_kind_roots = any(k.root for k in self.kinds)  # xml: a root per kind (0.6)
        for k in self.kinds:
            k.when_lines = tuple(k.whens)
            self._kind_index.setdefault(k.name, k)
            self._kinds_by_root.setdefault(k.root or self.root, []).append(k)

    @property
    def fields(self):
        """Every `field` line: the common ones, then each kind's, in line order."""
        out = [ln for ln in self.common if ln.is_field]
        for k in self.kinds:
            out.extend(k.fields)
        return out

    @property
    def field_locators(self):
        return {f.locator for f in self.fields}

    def kind(self, name):
        return self._kind_index.get(name)

    def kinds_for_root(self, root):
        """xml: the kinds considered for a document with this root element, in pack order."""
        return self._kinds_by_root.get(root, ())

    def lines(self, kind=None):
        """The lines that apply to a message of `kind`: common first, then the kind's."""
        return self.plan(kind).lines

    def plan(self, kind=None):
        key = kind.name if kind is not None else None
        plan = self._plans.get(key)
        if plan is None:
            if kind is None:
                plan = Plan(self.common, self.defaults, self.root, self.gates)
            else:
                plan = Plan(
                    self.common + kind.lines, self.defaults + kind.defaults, kind.root or self.root,
                    self.gates + kind.gates,
                )
            self._plans[key] = plan
        return plan

    def model_keys(self):
        """The union of mapped keys over common and all kinds, in first
        appearance order. A list is `<list>[]` and each key of its items
        `<list>[].<key>`, nested lists likewise (0.7)."""
        out = []
        seen = set()

        def add(name):
            if name not in seen:
                seen.add(name)
                out.append(name)

        if self.seq is not None:
            self.seq.model_keys(add)
            return out

        def walk(lines, prefix):
            for ln in lines:
                if ln.is_field:
                    add(prefix + ln.key)
                elif ln.is_group:
                    add(prefix + ln.list + "[]")
                    walk(ln.lines, prefix + ln.list + "[].")

        walk(self.common, "")
        for k in self.kinds:
            walk(k.lines, "")
        return out


def split_key(key):
    """-> (module, name). A model key with no `.` is a key of `core`; otherwise
    the text before the first `.` is the module name."""
    if "." in key:
        module, name = key.split(".", 1)
        return module, name
    return CORE, key


# One token: a double-quoted value, a `#` or an unterminated quote at the start
# of a token, or a run of non-blank characters.
_TOKEN = re.compile(r'''[ \t\n\r]*(?:"([^"]*)"|(["#])|([^ \t\n\r]+))''')
# Tokens are separated by space, tab, line feed and carriage return only; any
# other character, U+00A0 included, is part of a token (ruling 15 of 0.18).
_WS_SPLIT = re.compile(r"[ \t\n\r]+")
_OTHER_WS = re.compile(r"[\f\v\x1c-\x1f]")  # what `str.split` also splits an ASCII line at


def tokenize(line, *, source="pack.def", lineno=0, error=UnterminatedQuote):
    """Split one line into tokens. `#` starts a comment only at the start of a
    token (so `path/#text` is a locator, not a comment). Double quotes wrap a
    token containing whitespace; inside the quotes `#` is literal and there is
    no escape, so a quoted value cannot contain a double quote (0.5 rulings 6, 17)."""
    if '"' not in line:
        # No quoted token: whitespace-separated tokens up to the first that starts with `#`.
        if line.isascii() and _OTHER_WS.search(line) is None:
            toks = line.split()
        else:
            toks = [t for t in _WS_SPLIT.split(line) if t]
        if "#" not in line:
            return toks
        for k, tok in enumerate(toks):
            if tok[0] == "#":
                return toks[:k]
        return toks
    toks = []
    pos = 0
    while True:
        m = _TOKEN.match(line, pos)
        if m is None:
            return toks
        quoted, mark, bare = m.groups()
        if mark is not None:
            if mark == "#":
                return toks
            raise error("unterminated quote at %s:%d" % (source, lineno))
        toks.append(bare if quoted is None else quoted)
        pos = m.end()


def line_head(raw, source, lineno, seen, directives=PACK_DIRECTIVES, arg_counts=ARG_COUNTS):
    """The line checks 1 to 6 of ruling 5b of 0.11, the same for every pack:
    the quotes (`unterminated-quote`), the directive (`unknown-directive`),
    the number of arguments (`bad-directive`), a line that needs the `wire`
    line before it (`missing-wire`), a repeated header line (`bad-directive`),
    the `wire` value (`unknown-wire`) and the `distribution` value
    (`bad-directive`). `seen` holds the header values read so far and is
    updated. -> (directive, arguments), or None for a line with no token."""
    toks = tokenize(raw, source=source, lineno=lineno)
    if not toks:
        return None
    kw, args = toks[0], toks[1:]
    fmt = seen.get("format")
    where = "%s:%d" % (source, lineno)
    if kw not in directives or (kw == "key" and directives is PACK_DIRECTIVES and len(args) != 1):
        # `key <hex>` (0.13) is a line of a pack; `key` with another number of
        # arguments is the line of a model module (ruling 5 of 0.11)
        raise UnknownDirective("unknown directive %r at %s" % (kw, where), format=fmt)
    lo, hi = arg_counts[kw]
    if (len(args) < lo or (hi is not None and len(args) > hi)
            or (kw == "checksum" and len(args) == 2 and args[0] != "sum16")):  # ruling 12 of 0.13
        want = "%d" % lo if lo == hi else "%d or more" % lo if hi is None else "%d to %d" % (lo, hi)
        raise BadDirective(
            "directive %r takes %s argument(s), got %d at %s" % (kw, want, len(args), where), format=fmt
        )
    if kw in NEEDS_WIRE and "wire" not in seen:
        raise MissingWire("the line %r stands before the 'wire' line, at %s" % (kw, where), format=fmt)
    if kw in HEADER_ONCE:
        if kw in seen:
            raise BadDirective("a second %r line at %s" % (kw, where), format=fmt)
        if kw == "wire" and args[0] not in WIRES:
            raise UnknownWire("unknown wire %r (known: %s) at %s" % (args[0], ", ".join(WIRES), where), format=fmt)
        if kw == "wire" and args[0] not in BUILD_WIRES:  # spec 0.24, ruling 1c
            raise UnknownWire(
                "wire %r is a wire of the specification that this build does not have (this build has: %s) at %s"
                % (args[0], ", ".join(BUILD_WIRES) or "no wire", where),
                format=fmt,
            )
        if kw == "distribution" and args[0] not in DISTRIBUTIONS:
            raise BadDirective(
                "unknown distribution %r (known: %s) at %s" % (args[0], ", ".join(DISTRIBUTIONS), where),
                format=fmt,
            )
        seen[kw] = args[0]
    return kw, args


_XML_SEG = r'''[^@#<>="' \t\n/][^<>="' \t\n/]*'''
_XML_LOCATOR = re.compile(r"(?:" + _XML_SEG + r"/)*(?:@[^/]+|#text)\Z")


def parse_xml_locator(locator, *, allow_presence=False):
    """-> (path_segments, kind, name). kind: "attr" | "text" | "elem" (presence,
    residue only). Raises ValueError when not an xml locator."""
    if not allow_presence and _XML_LOCATOR.match(locator) is not None:  # the common case, checked in one pass
        parts = locator.split("/")
        last = parts.pop()
        return (tuple(parts), "attr", last[1:]) if last[0] == "@" else (tuple(parts), "text", None)
    if not locator:
        raise ValueError("empty locator")
    parts = locator.split("/")
    last = parts[-1]
    path = parts[:-1]
    if last.startswith("@"):
        kind, name = "attr", last[1:]
        if not name:
            raise ValueError("empty attribute name")
    elif last == "#text":
        kind, name = "text", None
    elif allow_presence:
        kind, name, path = "elem", None, parts
    else:
        raise ValueError("xml locator must end in @name or #text")
    for seg in path:
        if not seg or seg[0] in "@#" or any(ch in seg for ch in "<>=\"' \t\n"):
            raise ValueError("bad element name %r" % seg)
    return (tuple(path), kind, name)


def parse_geojson_locator(locator):
    """-> ("coord", index) | ("prop", name) | ("top", name)."""
    if locator.startswith("geometry."):
        slot = locator[len("geometry."):]
        idx = {"lon": 0, "lat": 1, "alt": 2}.get(slot)
        if idx is None:
            raise ValueError("geometry locator must be geometry.lon|lat|alt")
        return ("coord", idx)
    if locator.startswith("properties."):
        name = locator[len("properties."):]
        if not name:
            raise ValueError("empty property name")
        return ("prop", name)
    if "." in locator or locator in ("type", "geometry", "properties", ""):
        raise ValueError("not a geojson top-level member locator")
    return ("top", locator)


def parse_bits_locator(locator):
    """-> (word, pos, length). Raises ValueError when not a bits locator."""
    parts = locator.split(":")
    if len(parts) != 3:
        raise ValueError("bits locator must be <word>:<pos>:<len>")
    if not all(p.isascii() and p.isdigit() for p in parts):
        raise ValueError("bits locator parts are decimal digits")
    word, pos, length = (int(p) for p in parts)
    if length < 1:
        raise ValueError("len must be >= 1")
    return (word, pos, length)  # the caller checks len against what the transform allows


def parse_protobuf_locator(locator):
    """-> tuple of field numbers (each >= 1). A dot-path descends into nested
    submessages: '4.1' is field 1 of the submessage in field 4."""
    if not locator:
        raise ValueError("empty locator")
    nums = []
    for part in locator.split("."):
        if not (part.isascii() and part.isdigit()):
            raise ValueError("protobuf locator must be dot-separated field numbers: %r" % locator)
        num = int(part)
        if num < 1 or num > (1 << 29) - 1:
            raise ValueError("protobuf field number must be 1 to 536870911, got %d" % num)
        nums.append(num)
    return tuple(nums)


def parse_json_locator(locator):
    """-> tuple of member names. A `.` separates names; each but the last
    addresses a nested object. Raises ValueError when a name is empty."""
    names = locator.split(".")
    if any(n == "" for n in names):
        raise ValueError("json locator must be member names separated by '.'")
    return tuple(names)


def parse_locator(wire, locator):
    if wire == "xml":
        return parse_xml_locator(locator)
    if wire == "bits":
        return parse_bits_locator(locator)
    if wire == "protobuf":
        return parse_protobuf_locator(locator)
    if wire == "json":
        return parse_json_locator(locator)
    if wire == "text":
        return wire_text.parse_locator(locator)
    return parse_geojson_locator(locator)


class _LineBad(BadDirective):
    """A `bad-directive` of the line checks of ruling 5b of 0.11 (steps 7 to
    10): raised as the line is read. Every other `bad-directive` of a line is
    checked after the file is read."""


class _Deferred(Exception):
    """A `bad-directive` that needs the whole file: it is raised after the file
    is read and before `missing-distribution` (0.5 ruling 25)."""

    def __init__(self, err):
        Exception.__init__(self, str(err))
        self.err = err


def set_stop(transform, tname, text):
    """The field option `stop <code>` (0.22) on a fixed-size `chars` field:
    -> why it is refused (`bad-directive` after the file), or None."""
    if v15_family(tname) != "chars":
        return "field option 'stop' applies to a 'chars' field only"
    try:
        transform.stop = terminator_code(text, transform.charset)
    except ValueError as e:
        return "field option 'stop': %s" % e
    return None


def _parse_options(tokens, fmt, locator, where):
    """-> {option: value} for the tokens after a `field` line's transform."""
    out = {}
    i = 0
    while i < len(tokens):
        opt = tokens[i]
        if opt not in FIELD_OPTIONS:  # 0.6 ruling 12
            raise _LineBad("unknown field option %r at %s" % (opt, where), format=fmt, locator=locator)
        if opt in out:
            raise _LineBad("field option %r is given twice at %s" % (opt, where), format=fmt, locator=locator)
        if opt in ("via", "absent", "with", "stop"):
            # The token after `absent` is always its argument (ruling 11 of 0.9).
            if i + 1 >= len(tokens):
                raise _LineBad(
                    "field option %r takes %s at %s" % (
                        opt, "a table name" if opt == "via" else "a model key" if opt == "with" else
                        "a terminator code" if opt == "stop" else "a value", where),
                    format=fmt, locator=locator,
                )
            out[opt] = tokens[i + 1]
            i += 2
        else:
            out[opt] = True
            i += 1
    return out


def parse_absent(wire, transform, length, text, fmt, locator, where, coord=False):
    """`absent <value>[,<value>...]` (spec 0.9) -> (the sentinels as the wire
    compares them, the first as the wire writes it). `length` is the field's
    bit length on `bits`. The token `""` is the empty text. The values are a
    line check (ruling 5b of 0.11, step 10), except on a `bits` field longer
    than 64 bits: that is raised as a deferred `bad-directive` (ruling 4d)."""

    def bad(msg):
        return _LineBad("field option 'absent' %s, at %s" % (msg, where), format=fmt, locator=locator)

    values = text.split(",")
    if len(values) > 1 and "" in values:
        raise bad("has an empty value in its list %r" % text)
    tname = transform.name
    if wire == "bits" or (wire == "protobuf" and tname != "pstring"):
        if wire == "bits":
            if length > 64:
                raise _Deferred(bad("is on a bits field of %d bits; a sentinel is at most 64 bits" % length))
            width = length
        else:
            width = 32 if tname == "pfloat" else 64
        ints = []
        for v in values:
            try:
                ints.append(read_uint_text(v, width))  # ruling 2c of 0.11
            except ValueError as e:
                raise bad("value %s (an unsigned decimal integer below 2^%d)" % (e, width))
        first = ints[0]
        if wire == "protobuf":
            if tname == "pdouble":
                first = (transform.wire_type, first.to_bytes(8, "little"))
            elif tname == "pfloat":
                first = (transform.wire_type, first.to_bytes(4, "little"))
            else:
                first = (transform.wire_type, first)  # a varint, no zig-zag
        return frozenset(ints), first
    if wire == "protobuf":  # pstring: the text as written, `=` included (ruling 12)
        raws = [v.encode("utf-8") for v in values]
        return frozenset(raws), (transform.wire_type, raws[0])
    texts = []
    typed = []  # json, geojson: (text, "number" | "bool" | "string")
    for v in values:
        if v == "":  # the quoted empty token
            texts.append("")
            typed.append(("", "string"))
            continue
        forced = v[0] == "="
        if forced:
            v = v[1:]
            if v == "":
                raise bad("value '=' is empty after the '=' is stripped; only \"\" names the empty text")
        if wire == "xml":
            texts.append(v)
            continue
        if forced:
            typed.append((v, "string"))
        elif v == "null":
            raise bad("value 'null' cannot be held by a transform (ruling 13 of 0.9)")
        elif v == "true" or v == "false":
            typed.append((v, "bool"))
        elif _JSON_NUMBER.match(v):
            try:
                canon = format_float(float(v))
            except (ValueError, OverflowError):
                raise bad("value %r is number text that does not fit a finite double" % v)
            typed.append((canon, "number"))
        else:
            typed.append((v, "string"))
        texts.append(typed[-1][0])
    if wire == "xml":
        return frozenset(texts), texts[0]
    if coord:  # ruling 25 of 0.9: a `geometry.*` slot holds a number
        for t, how in typed:
            if how != "number":
                raise bad("value %r on a geojson coordinate is not number text" % t)
    if tname == "bool":
        for t, how in typed:
            if how != "bool":
                raise bad("value %r on the 'bool' transform is not true or false" % t)
    from .jsonio import dump_string

    t, how = typed[0]
    out = dump_string(t) if how == "string" else t
    return frozenset(texts), out


def _item_locator(wire, name, locator, in_group, where):
    """`.` addresses the item itself when it is a scalar (`json`, `geojson`,
    `protobuf`, inside a group): -> True when `locator` is that. Outside a
    group it is `bad-directive` (a case of an earlier version, ruling 5a of
    0.11)."""
    if locator != "." or wire not in ("json", "geojson", "protobuf"):
        return False
    if not in_group:
        raise _LineBad(
            "the locator '.' addresses a scalar item and is written inside a group, at %s" % where,
            format=name, locator=locator,
        )
    return True


def _check_transform(name, tname, locator, where):
    """Step 7 of ruling 5b of 0.11: the transform's name, then its parameters."""
    try:
        check_transform_text(tname)
    except UnknownTransformName:
        raise UnknownTransform("unknown transform %r at %s" % (tname, where), format=name, locator=locator)
    except ValueError as e:
        raise _LineBad("transform %r: %s, at %s" % (tname, e, where), format=name, locator=locator)


def _bad_locator(wire, where, name, locator, why):
    return BadLocator("bad %s locator at %s: %s" % (wire, where, why), format=name, locator=locator)


def _fixed_bits_locator(name, locator, where):
    """A `<word>:<pos>:<len>` locator of a fixed-layout pack (ruling 5a of
    0.11). A bit length alone is a line of a sequential pack: `bad-directive`."""
    if locator.isascii() and locator.isdigit():
        raise _LineBad(
            "a bit length for a locator is a line of a sequential pack, at %s" % where,
            format=name, locator=locator,
        )
    try:
        return parse_bits_locator(locator)
    except ValueError as e:
        raise _bad_locator("bits", where, name, locator, e)


def _resolve_field(wire, name, key, locator, tname, options, source, lineno, deferred, in_group=False):
    """-> Field for one field line, or None when the line fails a check that
    is made after the file is read (added to `deferred`). The line checks come
    in the order of ruling 5b of 0.11: the transform's name and parameters,
    the locator's form, the field options, the values of `absent`. A transform
    on a wire it is not valid for, a bit length the transform does not allow
    and an option where it does not apply are checked after the file. Inside
    a group the locator is relative to one item (0.7)."""
    where = "%s:%d" % (source, lineno)
    if wire == "tlv":  # 0.13: every transform check of the tlv wire is made after the file (ruling 8)
        return _resolve_tlv_field(name, key, locator, tname, options, where, lineno, deferred)
    _check_transform(name, tname, locator, where)
    valid = transform_valid_on(tname, wire)
    later = []

    def defer(msg):
        later.append((lineno, BadDirective("%s, at %s" % (msg, where), format=name, locator=locator)))

    if not valid:
        defer("transform %r is not valid on the %s wire" % (tname, wire))

    # step 8: the locator
    if wire == "text":
        try:
            loc = wire_text.parse_locator(locator)
        except ValueError as e:  # ruling 14 of 0.10: `bad-directive` on `text`
            raise _LineBad("bad text locator at %s: %s" % (where, e), format=name, locator=locator)
        if loc[0] == "p" and loc[1] == loc[2]:
            raise _LineBad("a pair whose two indexes are equal, at %s" % where, format=name, locator=locator)
    elif wire == "bits":
        if locator.startswith(uper.PREFIXES):
            # 0.19 ruling 9: a UPER locator outside a UPER pack is `bad-directive`,
            # checked after the file is read, not a locator form of step 8. The
            # field options are still a line check (step 9).
            _parse_options(options, name, locator, where)
            deferred.append((lineno, BadDirective(
                "a UPER locator (%r) outside a UPER pack ('layout sequential', 'encoding uper'), at %s"
                % (locator, where), format=name, locator=locator,
            )))
            return None
        if locator.startswith("until:"):
            # 0.22: `until:<code>` is a locator of sequential layouts only,
            # `bad-directive` after the file; the options are still a line check
            _parse_options(options, name, locator, where)
            deferred.append((lineno, BadDirective(
                "the locator %r is for sequential layouts only, at %s" % (locator, where), format=name, locator=locator,
            )))
            return None
        loc = _fixed_bits_locator(name, locator, where)
    elif _item_locator(wire, name, locator, in_group, where):
        loc = ()
    else:
        try:
            if wire == "protobuf":
                loc = parse_protobuf_locator(locator)
            elif in_group and wire == "geojson":
                loc = parse_json_locator(locator)  # an item of a geojson list is addressed as on `json`
            else:
                loc = parse_locator(wire, locator)
        except ValueError as e:
            raise _bad_locator(wire, where, name, locator, e)

    # step 9: the field options
    opts = _parse_options(options, name, locator, where)
    via = opts.get("via")
    if wire == "bits":
        if "swap" in opts and tname in ("sixbit", "hex"):  # 0.14, 0.22
            defer("field option 'swap' does not apply to the '%s' transform" % tname)
        elif "swap" in opts and loc[2] % 8 != 0:
            defer("field option 'swap' needs a bit length that is a multiple of 8, got %d" % loc[2])
        if "trim" in opts and tname not in ("ascii", "sixbit"):  # ruling 5 of 0.14
            defer("field option 'trim' applies to the 'ascii' and 'sixbit' transforms only")
        if "swap" in opts and v15_family(tname) in ("chars", "octal"):  # 0.15
            defer("field option 'swap' does not apply to the '%s' transform" % v15_family(tname))
    else:
        for opt in (("swap", "trim") if wire == "text" else ("swap", "trim", "optional")):
            if opt in opts:
                defer("field option %r applies to the bits wire only" % opt)
        if wire == "text" and "absent" in opts:  # ruling 16 of 0.10
            defer("field option 'absent' is not a field option of the text wire")
        if "stop" in opts:  # 0.22: a `chars` field of the bits wire
            defer("field option 'stop' applies to a 'chars' field of the bits wire only")

    transform = None
    if valid:
        if wire == "bits":
            length = loc[2]
            if not bits_len_allowed(tname, length):
                defer("transform %s does not allow a bit length of %d" % (tname, length))
            transform = build_bits_transform(tname, length)
            if "trim" in opts and tname in ("ascii", "sixbit"):
                transform.trim = True
            if "stop" in opts:  # 0.22
                why = set_stop(transform, tname, opts["stop"])
                if why is not None:
                    defer(why)
        elif wire == "protobuf":
            transform = PROTOBUF_TRANSFORMS[tname]
        elif wire == "text":
            transform = wire_text.build_transform(tname)
            if (loc[0] == "p") != transform.pair:
                defer("transform %s %s" % (tname, "takes a pair <i>+<j>" if transform.pair else "does not take a pair"))
        elif v15_family(tname) is not None:
            transform = build_v15_text(tname)  # `scale`, `iscale` (0.15)
        else:
            transform = (JSON_TRANSFORMS if wire == "json" else TRANSFORMS)[tname]

    f = None
    if transform is not None:
        f = Field(key, locator, transform, loc, lineno, via=via, swap="swap" in opts, optional="optional" in opts,
                  with_key=opts.get("with"))
        # step 10: the values of `absent`
        if wire != "text":
            try:
                _with_absent(f, wire, opts, name, where)
            except _Deferred as d:
                later.append((lineno, d.err))
    if later:
        deferred.extend(later)
        return None
    return f


def _with_absent(f, wire, opts, fmt, where):
    """Set the sentinels of the `absent` option on `f`, if it has one. -> f."""
    f.with_key = opts.get("with")
    text = opts.get("absent")
    if text is not None:
        length = f.loc[2] if wire == "bits" else None
        coord = wire == "geojson" and len(f.loc) == 2 and f.loc[0] == "coord" and isinstance(f.loc[1], int)
        f.absent, f.absent_out = parse_absent(wire, f.transform, length, text, fmt, f.locator, where, coord)
    return f


def _resolve_tlv_field(name, key, locator, tname, options, where, lineno, deferred):
    """-> Field for one `field` line of a `tlv` pack (0.13), or None when it
    fails a check (added to `deferred`). The syntax of the field options stays
    a line check (ruling 5b of 0.11, step 9). The path, the transform (a name
    not in the table of the tlv wire, scale parameters that do not parse:
    ruling 8), the size against the transform, and the options `swap`,
    `trim`, `optional` and `absent` (ruling 10) are `bad-directive` after the
    file is read."""
    opts = _parse_options(options, name, locator, where)
    later = []

    def bad(msg):
        later.append((lineno, BadDirective("%s, at %s" % (msg, where), format=name, locator=locator)))

    transform = None
    try:
        loc = wire_tlv.parse_path(locator)
    except ValueError as e:
        bad("bad tlv path: %s" % e)
        loc = None
    if loc is not None:
        try:
            transform = wire_tlv.build_transform(tname, loc[1])
        except wire_tlv.ParamError as e:  # ruling 8 of 0.13
            bad("transform %r: parameters that do not parse: %s" % (tname, e))
        except ValueError as e:
            bad(str(e))
    for opt in ("swap", "trim", "optional", "stop"):
        if opt in opts:
            bad("field option %r is not an option of the tlv wire" % opt)
    if "absent" in opts:  # ruling 10 of 0.13: an empty value is already absent on tlv
        bad("field option 'absent' is not an option of the tlv wire (ruling 10 of 0.13)")
    if later:
        deferred.extend(later)
        return None
    return Field(key, locator, transform, loc, lineno, via=opts.get("via"), with_key=opts.get("with"))


def _resolve_tlv_when(name, locator, value, where, lineno, const):
    """-> When or Const for one line of a `tlv` pack (0.13, "`when` and
    `const`"); every failure is `bad-directive` after the file is read."""
    directive = "const" if const else "when"

    def bad(msg):
        return _Deferred(BadDirective("%s, at %s" % (msg, where), format=name, locator=locator))

    try:
        tags, size = wire_tlv.parse_path(locator)
    except ValueError as e:
        raise bad("bad tlv path: %s" % e)
    if value == "present":
        if const:
            raise bad("a 'const' value is written, so cannot be 'present'")
        if size is not None:
            raise bad("'when <path> present' takes no size")
        return When(locator, (tags, None), present=True, lineno=lineno)
    if size is None:
        raise bad("a numeric '%s' needs a size ':<n>'" % directive)
    try:
        n = read_uint_text(value)  # an unsigned decimal integer (ruling 2c of 0.11)
    except ValueError:
        raise bad("a tlv '%s' value is an unsigned decimal integer%s, got %r"
                  % (directive, "" if const else " or 'present'", value))
    if n >= 1 << (8 * size):
        raise bad("'%s' value %d does not fit %d byte(s)" % (directive, n, size))
    cls = Const if const else When
    return cls(locator, (tags, size), value=n, lineno=lineno)


class _WrongGroupLocator(ValueError):
    """A group locator of the right form that is not valid for its wire:
    `bad-directive` (ruling 37 of 0.21), not `bad-locator` (5a of 0.11)."""


def _resolve_group(wire, name, list_name, locator, options, source, lineno, in_group, deferred):
    """-> Group for one `group` line (spec 0.7, "Group locators and
    item-relative locators, by wire"). The locator is a line check
    (`bad-locator`; `bad-directive` on `text`); the options are checked after
    the file is read (ruling 5b of 0.11): a group whose options fail is
    returned `broken`, its error added to `deferred`."""
    where = "%s:%d" % (source, lineno)
    problems = []

    def bad(msg):
        problems.append((lineno, BadDirective("%s at %s" % (msg, where), format=name, locator=locator)))

    if wire == "bits" and locator in ("count", "while", "more", "fx", "rest", "last"):  # 0.21 adds two
        raise _LineBad("a group with %r is a line of a sequential pack at %s" % (locator, where),
                           format=name, locator=locator)
    # the locator
    if wire == "text":
        try:
            loc = (wire_text._index(locator),)
        except ValueError as e:
            raise _LineBad("bad text group locator at %s: %s" % (where, e), format=name, locator=locator)
    elif wire == "bits":
        parts = locator.split(":")
        if len(parts) != 2 or not all(p.isascii() and p.isdigit() for p in parts):
            raise _bad_locator("bits", where, name, locator, "a group locator is <word>:<pos>")
        loc = (int(parts[0]), int(parts[1]))
    else:
        try:
            if wire == "xml":
                loc = parse_xml_locator(locator, allow_presence=True)
                if loc[1] != "elem":
                    raise _WrongGroupLocator("an xml group locator is a path of element names")
                loc = loc[0]
            elif wire == "protobuf":
                if locator in ("rest", "last"):  # the sequential group modes of 0.21
                    raise _WrongGroupLocator("%r is a group mode of a sequential pack, not a field path" % locator)
                loc = parse_protobuf_locator(locator)
            elif wire == "json" or in_group:
                loc = parse_json_locator(locator)
            else:
                loc = parse_geojson_locator(locator)
                if loc[0] == "coord":
                    raise ValueError("a geojson group locator is a properties member or a top-level member")
        except _WrongGroupLocator as e:  # 0.21 ruling 37: not valid for its wire, after the file
            bad("bad %s group locator: %s" % (wire, e))
            loc = ()
        except ValueError as e:
            raise _bad_locator(wire, where, name, locator, e)

    # the options, after the file is read
    opts = {}
    i = 0
    while i < len(options):
        opt = options[i]
        if opt not in ("item", "count") or opt in opts or i + 1 >= len(options):
            bad("a 'group' line is: group <list> <locator> [item <bits>] [count <locator | n>]; got %r" % opt)
            break
        opts[opt] = options[i + 1]
        i += 2
    g = Group(list_name, locator, loc, lineno)
    if wire == "text":
        if "count" in opts:
            bad("'count' on a 'group' is bits only")
        item = opts.get("item")
        if item is None or not (item.isascii() and item.isdigit()) or int(item) < 1:
            bad("a text group is: group <list> <i> item <n>, n from 1")
        else:
            g.item_bits = int(item)
    elif wire != "bits":
        for opt in ("count", "item"):
            if opt in opts:
                bad("%r on a 'group' is bits only" % opt)
    else:
        item = opts.get("item")
        count = opts.get("count")
        if item is None:
            bad("a bits group needs 'item <bits>'")
        elif not (item.isascii() and item.isdigit()) or int(item) < 1:
            bad("'item' takes a number of bits, got %r" % item)
        else:
            g.item_bits = int(item)
        if count is None:
            bad("a bits group needs 'count <count-locator | n>'")
        elif count.isascii() and count.isdigit():
            g.count_n = int(count)
        else:
            try:
                g.count_loc = parse_bits_locator(count)
            except ValueError as e:
                bad("'count' takes a number or a <word>:<pos>:<len> locator (%s)" % e)
            else:
                if g.count_loc[2] > 64:
                    bad("a count locator is at most 64 bits")
    if problems:
        g.broken = True
        deferred.extend(problems[:1])
    return g


def _open_group(proto, lineno, stack, common_lists, kind_lists, fmt, where, deferred):
    """-> a new Group like `proto`, opened at `lineno`. Within one scope
    (common plus one kind, or one group body) a list is mapped by at most one
    group (`bad-directive`)."""
    name = proto.list
    if stack:
        parent = stack[-1]
        seen = parent._seen
        if seen is None:
            seen = parent._seen = set()
        twice = name in seen
    else:
        seen = kind_lists if kind_lists is not None else common_lists
        twice = name in common_lists or (kind_lists is not None and name in kind_lists)
    if twice:  # checked after the file is read (ruling 5b of 0.11)
        deferred.append((lineno, BadDirective(
            "list %s is mapped by two groups of one scope, at %s" % (name, where), format=fmt, locator=proto.locator
        )))
    seen.add(name)
    g = Group(name, proto.locator, proto.loc, lineno, proto.item_bits, proto.count_loc, proto.count_n)
    g.broken = proto.broken
    return g


def _resolve_when(wire, name, locator, value, source, lineno, deferred, directive="when", in_group=False):
    """-> When for one `when` line (spec 0.5, "`when` values, by wire"), or
    Const for one `const` line, whose value is typed as a `when` value is and
    is never `present` (spec 0.6, "Constants"). The locator is a line check;
    the value is checked after the file is read (ruling 5b of 0.11): a value
    that fails adds its error to `deferred` and gives None."""
    where = "%s:%d" % (source, lineno)
    const = directive == "const"
    if wire == "tlv":
        return _resolve_tlv_when(name, locator, value, where, lineno, const)

    def make(val, forced=False):
        if const:
            return Const(locator, loc, value=val, forced=forced, lineno=lineno)
        return When(locator, loc, value=val, forced=forced, lineno=lineno)

    def later(msg):
        deferred.append((lineno, BadDirective("%s, at %s" % (msg, where), format=name, locator=locator)))

    if locator == "." and wire in ("json", "geojson", "protobuf"):
        raise _LineBad(
            "the locator '.' is for a 'field' of a scalar item, not a '%s', at %s" % (directive, where),
            format=name, locator=locator,
        )
    if wire == "bits":
        loc = _fixed_bits_locator(name, locator, where)
    else:
        try:
            loc = parse_json_locator(locator) if in_group and wire == "geojson" else parse_locator(wire, locator)
        except ValueError as e:
            if wire == "text":  # ruling 14 of 0.10
                raise _LineBad("bad text locator at %s: %s" % (where, e), format=name, locator=locator)
            raise _bad_locator(wire, where, name, locator, e)
    if const and value == "present":
        return later("a 'const' value is written, so cannot be 'present' (text wires: '=present')")
    # 0.20: a list of values, and on `bits` and `protobuf` ranges (checked after
    # the file). Not on `text`: there a comma is the default delimiter, which a
    # value may not hold (Version 0.10), and the value stays one literal text.
    if not const and wire != "text":
        if wire == "bits" and loc[2] > 64:
            return later("a '%s' of %d bits; at most 64" % (directive, loc[2]))
        width = loc[2] if wire == "bits" else 64 if wire == "protobuf" else None

        def err(msg):
            return BadDirective("%s, at %s" % (msg, where), format=name, locator=locator)

        try:
            return when_values(wire, value, width, err, lambda msg: _Deferred(err(msg)),
                               lambda **kw: When(locator, loc, lineno=lineno, **kw))
        except _Deferred as d:
            deferred.append((lineno, d.err))
            return None
        except BadDirective as e:
            deferred.append((lineno, e))
            return None
    if wire == "bits":
        if loc[2] > 64:
            return later("a '%s' of %d bits; at most 64" % (directive, loc[2]))
        try:
            n = read_uint_text(value)  # ruling 2c of 0.11
        except ValueError:
            return later("a bits '%s' value is an unsigned decimal integer, got %r" % (directive, value))
        if n >= (1 << loc[2]):
            return later("'%s' value %d does not fit %d bit(s)" % (directive, n, loc[2]))
        return make(n)
    if wire == "protobuf":
        if value == "present":
            return When(locator, loc, present=True, lineno=lineno)
        try:
            n = read_uint_text(value)
        except ValueError:
            return later("a protobuf '%s' value is %san unsigned decimal integer, got %r"
                         % (directive, "" if const else "'present' or ", value))
        if n >= (1 << 64):
            return later("'%s' value %d does not fit a varint" % (directive, n))
        return make(n)
    if value == "present":
        return When(locator, loc, present=True, lineno=lineno)
    if value.startswith("="):
        return make(value[1:], forced=True)
    return make(value)


def split_list(text, bad):
    """0.20 ruling 16: the comma-separated items of a list; an empty item is
    `bad-directive`, and only a lone `""` is the empty text."""
    if text == "":
        return [""]
    items = text.split(",")
    if "" in items:
        raise bad("an empty value in the list %r" % text)
    return items


def when_values(wire, text, width, bad, deferred, make):
    """-> When for the value list of one `when` line (0.20). `width` is the
    number of bits a value must fit on `bits` and `protobuf`, None on a text
    wire. `bad` builds the error raised at once, `deferred` the one raised
    after the file is read (a value that does not fit)."""
    if text == "present" and wire != "bits":
        return make(present=True)
    items = split_list(text, bad)
    if width is None:  # a text wire: `..` is literal, a leading `=` is stripped from each value
        if "present" in items:
            raise bad("'present' may not be combined with other values")
        texts = []
        forced = []
        for v in items:
            if v.startswith("="):
                texts.append(v[1:])
                forced.append(True)
            else:
                texts.append(v)
                forced.append(False)
        return make(value=texts[0], forced=forced[0], texts=frozenset(texts))
    if "present" in items:
        raise bad("'present' may not be combined with other values" if wire == "protobuf"
                  else "a bits 'when' value is an unsigned decimal integer, got 'present'")
    ranges = []
    late = None
    for v in items:
        parts = v.split("..")
        if len(parts) > 2 or not all(p.isascii() and p.isdigit() for p in parts):
            raise bad("a %s 'when' value is an unsigned decimal integer or a range <lo>..<hi>, got %r"
                      % (wire, v))
        lo = int(parts[0])
        hi = int(parts[-1])
        if lo > hi:
            raise bad("the range %r has its low end above its high end" % v)
        if hi >= (1 << width) and late is None:
            late = deferred("'when' value %r does not fit %d bit(s)" % (v, width))
        ranges.append((lo, hi))
    if late is not None:
        raise late
    return make(value=ranges[0][0], ranges=tuple(ranges))


def _check_bits_groups(word_bytes, common, kinds, deferred, source, name, common_gates=()):
    """The checks of a `bits` pack's groups that need the whole file (0.7):
    a `field`, `when`, `const` or second group whose bits lie at or beyond the
    start of a group counted by a locator; a count locator that reaches its
    group's start; a field of an item, or a nested group with a fixed count,
    that reaches beyond `item <bits>`. Each is `bad-directive`. A group whose
    options failed is left out: its own error is reported."""
    if word_bytes is None or not (word_bytes.isascii() and word_bytes.isdigit()) or int(word_bytes) < 1:
        return  # reported with the whole-file checks
    word_bits = int(word_bytes) * 8

    def bad(ln, msg):
        deferred.append((ln.lineno, BadDirective(
            "%s, at %s:%d" % (msg, source, ln.lineno), format=name, locator=ln.locator
        )))

    def start(loc):
        return loc[0] * word_bits + loc[1]

    def ok(lines):
        return [ln for ln in lines if not (ln.is_group and ln.broken)]

    def scope(lines, item_bits):
        lines = ok(lines)
        counted = []
        for ln in lines:
            if ln.is_group:
                s = start(ln.loc)
                if ln.count_loc is not None:
                    counted.append(ln)
                    if start(ln.count_loc) + ln.count_loc[2] > s:
                        bad(ln, "the count locator reaches the group's start")
                    if item_bits is not None and start(ln.count_loc) + ln.count_loc[2] > item_bits:
                        bad(ln, "the count locator of a nested group reaches beyond 'item %d'" % item_bits)
                elif item_bits is not None and s + ln.count_n * ln.item_bits > item_bits:
                    bad(ln, "a nested group with a fixed count reaches beyond 'item %d'" % item_bits)
            elif item_bits is not None and start(ln.loc) + ln.loc[2] > item_bits:
                bad(ln, "the line reaches beyond 'item %d'" % item_bits)
        for g in counted:
            gs = start(g.loc)
            for ln in lines:
                if ln is g:
                    continue
                if ln.is_group:
                    s = start(ln.loc)
                    beyond = s >= gs or (ln.count_n is not None and s + ln.count_n * ln.item_bits > gs)
                else:
                    beyond = start(ln.loc) + ln.loc[2] > gs
                if beyond:
                    bad(ln, "the line's bits lie at or beyond the start of the group counted by %s" % g.locator)

    def fixed_size(g):
        """The size of every item of `g`, or None when a group counted by a
        locator is nested in it."""
        for ln in g.lines:
            if ln.is_group and (ln.count_loc is not None or fixed_size(ln) is None):
                return None
        return g.item_bits

    def items_cover(lines, gates):
        # ruling 9 of 0.17: a gate whose bits lie within the items of a group
        # with a fixed count (whose item bits are known) of the same scope
        for g in lines:
            if not g.is_group or g.count_loc is not None:
                continue
            size = fixed_size(g)
            if size is None:
                continue
            gs = start(g.loc)
            ge = gs + g.count_n * size
            for gate in gates:
                s0 = start(gate.loc)
                if s0 < ge and s0 + gate.loc[2] > gs:
                    bad(gate, "the gate's bits lie within the items of group %s" % g.list)

    def bodies(lines):
        for ln in ok(lines):
            if ln.is_group:
                if ln.gates:
                    items_cover(ln.lines, ln.gates)
                scope(ln.lines + ln.gates, ln.item_bits)  # a gate locator is checked as a field is (0.17 ruling 2)
                bodies(ln.lines)

    def has_counted(lines):
        return any(ln.is_group and not ln.broken and ln.count_loc is not None for ln in lines)

    bodies(common)
    for k in kinds:
        bodies(k.lines)
    if common_gates:
        items_cover(common, common_gates)
    for k in kinds:
        if k.gates:
            items_cover(common, k.gates)
            items_cover(k.lines, k.gates)
        if common_gates:
            items_cover(k.lines, common_gates)
    common_counted = has_counted(common)
    if common_counted:
        scope(common + list(common_gates), None)
    for k in kinds:  # a scope is common plus one kind
        if common_counted or has_counted(k.lines):
            scope(common + list(common_gates) + k.lines + k.gates, None)


def _text_directive(kw, value, seen, current, fmt, lineno, where):
    """`delimiter`, `start` and `checksum` (0.10): each at most once, before the
    first `kind`, with a value of its form. Whether the wire is `text` is
    checked after the file is read (ruling 10 of 0.10), as are their relations
    to one another (`wire_text.check_pack`)."""

    def bad(msg):
        return BadDirective("%s, at %s" % (msg, where), format=fmt, locator=kw)

    if kw in seen:
        raise bad("%r is given twice" % kw)
    seen[kw] = (value, lineno)
    if current is not None:
        raise bad("%r after the first 'kind'" % kw)
    if kw == "delimiter":
        ok = value == "none" or (len(value) == 1 and "!" <= value <= "~" and value != '"')
    elif kw == "start":
        ok = value != "" and all("!" <= c <= "~" for c in value)
    else:
        ok = value in ("none", "nmea")
    if not ok:
        raise bad("%r does not take the value %r" % (kw, value))


def _check_tlv_lines(lines, wire, deferred, source, fmt):
    """`tag`, `length`, `key` and `checksum sum16 <tag>` (0.13), checked after
    the file is read: on `tlv` each at most once, before the first `kind`, with
    a value of its form; on another wire `bad-directive`. `lines` holds
    (directive, arguments, line number, after a `kind`, inside a group).
    -> {directive: (value, line number)} of the valid lines."""
    seen = {}
    out = {}
    for kw, args, lineno, after_kind, in_group in lines:
        where = "%s:%d" % (source, lineno)

        def bad(msg):
            deferred.append((lineno, BadDirective("%s, at %s" % (msg, where), format=fmt, locator=kw)))

        if wire != "tlv":
            bad("%r is a directive of the tlv wire" % kw)
            continue
        if kw in seen:
            bad("%r is given twice" % kw)
            continue
        seen[kw] = lineno
        if in_group:
            bad("%r inside a group" % kw)
            continue
        if after_kind:
            bad("%r after the first 'kind'" % kw)
            continue
        value = None
        if kw == "checksum":
            if len(args) == 2 and args[0] == "sum16":
                value = wire_tlv.read_tag(args[1])
        elif len(args) == 1:
            a = args[0]
            if kw == "tag":
                value = a if a in wire_tlv.TAG_FORMS else None
            elif kw == "length":
                value = a if a in wire_tlv.LENGTH_FORMS else None
            else:
                value = bytes.fromhex(a) if wire_tlv.valid_key(a) else None
        if value is None:
            bad("%r does not take %r" % (kw, " ".join(args)))
            continue
        out[kw] = (value, lineno)
    return out


def armor_line(args, lines, after_kind, lineno):
    """Record an `armor` line (0.14), whose one argument `line_head` checked
    (ruling 13 of 0.14). Whether it fits its pack is checked after the file
    is read (`check_armor`). Shared by the sequential layout."""
    lines.append((args, lineno, after_kind))


def check_armor(lines, wire, bit_order, deferred, source, fmt):
    """`armor <hex | sixbit>` (0.14), after the file is read: on the `bits`
    wire only, at most once, before the first `kind`, with one argument that
    is `hex` or `sixbit` (its count is a line check); `armor sixbit` needs `bit_order msb`, wherever the
    lines stand. Each failure is appended to `deferred`. -> the armor."""
    value = "hex"
    for k, (args, lineno, after_kind) in enumerate(lines):
        where = "%s:%d" % (source, lineno)

        def bad(msg):
            deferred.append((lineno, BadDirective("%s, at %s" % (msg, where), format=fmt, locator="armor")))

        if wire != "bits":
            bad("'armor' is a directive of the bits wire")
        elif k > 0:
            bad("'armor' is given twice")
        elif after_kind:
            bad("'armor' after the first 'kind'")
        elif args[0] not in ARMORS:
            bad("armor must be one of %s, got %r" % (", ".join(ARMORS), args[0]))
        else:
            value = args[0]
            if value == "sixbit" and bit_order != "msb":
                bad("'armor sixbit' needs 'bit_order msb'")
    return value


def check_encoding(lines, wire, sequential, bit_order, armor, deferred, source, fmt):
    """`encoding <none | uper>` (0.19), after the file is read (its one argument
    is a line check, `line_head`): on the `bits` wire only, whatever its value
    (ruling 18); at most once; before the first `kind`; `none` or `uper`;
    `uper` needs `layout sequential` and `bit_order msb`, and not `armor
    sixbit` (ruling 11). `lines` holds (value, line number, after a kind).
    Each failure is appended to `deferred`. -> the encoding. Shared by the
    sequential layout."""
    value = "none"
    for k, (arg, lineno, after_kind) in enumerate(lines):
        where = "%s:%d" % (source, lineno)

        def bad(msg):
            deferred.append((lineno, BadDirective("%s, at %s" % (msg, where), format=fmt, locator="encoding")))

        if wire != "bits":
            bad("'encoding' is a directive of the bits wire")
        elif k > 0:
            bad("'encoding' is given twice")
        elif after_kind:
            bad("'encoding' after the first 'kind'")
        elif arg not in ("none", "uper"):
            bad("encoding must be none or uper, got %r" % arg)
        elif arg == "uper" and not sequential:
            bad("'encoding uper' needs 'layout sequential'")
        elif arg == "uper" and bit_order != "msb":
            bad("'encoding uper' needs 'bit_order msb'")
        elif arg == "uper" and armor == "sixbit":
            bad("'encoding uper' with 'armor sixbit'")
        else:
            value = arg
    return value


def _find_wire(text, directive="wire"):
    """The value of the first `wire` (or other) line of two tokens, read
    without checking the file: what chooses the parser of a sequential pack."""
    for raw in text.splitlines():
        if directive not in raw:
            continue
        try:
            toks = tokenize(raw)
        except UnterminatedQuote:
            continue
        if len(toks) == 2 and toks[0] == directive:
            return toks[1]
    return None


def _check_group_paths(wire, common, kinds, deferred, source, name):
    """Ruling 21 of 0.7: a `field` whose locator passes through the path of a
    `group` of the same scope (common plus one kind, or one group body) is
    `bad-directive`."""
    xml = wire == "xml"

    def prefixes(f):
        """The paths a group must not have for field `f`: every proper prefix of
        its member or field-number path; on xml every prefix of its element path."""
        loc = f.loc
        if xml:
            path = loc[0]
            return [path[:n] for n in range(1, len(path) + 1)]
        if not loc or loc[0] in ("prop", "top", "coord"):
            return ()
        return [loc[:n] for n in range(1, len(loc))]

    def report(f, g):
        deferred.append((f.lineno, BadDirective(
            "the field's locator passes through the path of group %s, at %s:%d" % (g.list, source, f.lineno),
            format=name, locator=f.locator,
        )))

    def check(fields, groups):
        paths = {tuple(g.loc): g for g in groups if isinstance(g.loc, tuple)}
        for f in fields:
            for pre in prefixes(f):
                g = paths.get(tuple(pre))
                if g is not None:
                    report(f, g)
                    break

    def split(lines):
        return [ln for ln in lines if ln.is_field], [ln for ln in lines if ln.is_group]

    def bodies(groups):
        for g in groups:
            fields, inner = split(g.lines)
            if inner:
                check(fields, inner)
                bodies(inner)

    cf, cg = split(common)
    check(cf, cg)
    bodies(cg)
    common_pre = None
    for k in kinds:
        kf, kg = split(k.lines)
        if cg:
            check(kf, cg)
        if kg:
            check(kf, kg)
            if common_pre is None:  # every forbidden path of the common fields, once
                common_pre = {}
                for f in cf:
                    for pre in prefixes(f):
                        common_pre.setdefault(tuple(pre), f)
            for g in kg:
                f = common_pre.get(tuple(g.loc)) if isinstance(g.loc, tuple) else None
                if f is not None:
                    report(f, g)
            bodies(kg)


def _check_scalar_body(g, fmt, where, deferred):
    """Ruling 13 of 0.7: a `protobuf` group body is scalar only when every
    line is `.`; a body mixing `.` with paths is `bad-directive`."""
    scalar = False
    other = False
    for ln in g.lines:
        if ln.is_field and ln.locator == ".":
            scalar = True
        else:
            other = True
    if scalar and other:
        deferred.append((g.lineno, BadDirective(
            "the body of group %s mixes '.' with paths, at %s" % (g.list, where), format=fmt, locator=g.locator
        )))
    # Rulings 4 and 17 of 0.18: a packed group (a body of `.` fields with numeric
    # transforms only) takes no `with`.
    if scalar and not other and all(ln.transform.wire_type != 2 for ln in g.lines):
        for ln in g.lines:
            if ln.with_key is not None:
                deferred.append((ln.lineno, BadDirective(
                    "field option 'with' on a '.' item of the packed group %s, at %s" % (g.list, where),
                    format=fmt, locator=ln.locator,
                )))
                break


def parse_pack_text(text, *, source="pack.def"):
    """Parse one pack.def, in the order of ruling 5b of 0.11. Each line is
    checked as it is read (`line_head`, then the transform, the locator, the
    field options and the values of `absent`), and the first failure is the
    error of the load. After the file: `missing-format`, `missing-version`,
    `missing-wire`; then the checks that need the whole file, the first in
    line order (`bad-directive`), the load-time `missing-root`, the missing
    `word_bytes`; then `missing-distribution`, `missing-source`. The checks
    against the loaded modules and `duplicate-format` are `Engine.add`'s."""
    # A build without `bits` does not have the sequential parser; the pack is
    # read below, where its `wire` line is `unknown-wire` (spec 0.24, ruling 1c).
    if "bits" in BUILD_WIRES and _find_wire(text) == "bits" and _find_wire(text, "layout") == "sequential":
        from .seqdef import parse_seq_pack_text
        return parse_seq_pack_text(text, source=source)
    seen = {}            # the header values: `format`, `version`, `wire`, `distribution`
    once = {}            # `word_bytes`, `bit_order`, `layout`
    wire = None
    typemap = []
    sources = []
    uses = []
    common = []
    kinds = []
    kind_names = set()
    key_refs = []
    defaults = []
    tables = {}
    common_gates = []
    value_checks = []
    via_fields = []
    deferred = []        # (lineno, BadDirective)
    elements = []        # (Element, the Kind holding it or None) (0.18)
    root = None
    current = None       # the Kind being read, None before the first `kind`
    first_when = None    # (lineno, locator) of the first `when`, for the no-kind check
    stack = []           # the open groups, outermost first (0.7)
    gstack = []          # the open gate blocks, outermost first (0.17); always inside the open groups
    common_lists = set() # lists mapped by a common group
    kind_lists = None    # lists mapped by a group of the kind being read
    # A generated pack repeats a few thousand distinct lines over a million
    # times: a `field` or `group` line is resolved once per distinct text (and
    # per position, inside a group or not). Lines of one text share one Field.
    # A line enters the memo only when it passed every check.
    field_memo = ({}, {})
    group_memo = ({}, {})
    group_refs = set()   # list names already in key_refs
    text_dirs = {}       # text (0.10): `delimiter`, `start`, `checksum` -> (value, lineno)
    tlv_lines = []       # tlv (0.13): `tag`, `length`, `key`, `checksum sum16 <tag>` as (kw, args, lineno, after a kind, in a group)
    armor = []           # bits (0.14): the `armor` lines, checked after the file (`check_armor`)
    encoding = []        # 0.19: the `encoding` lines, checked after the file (`check_encoding`)
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if gstack:
            proto = None  # a line of a gate block: read in full below (lines of a block are not shared)
        elif stack:
            # `end` stands after the `wire` line (5b of 0.11); a block `ext` or
            # `choice` opened before it (ruling 25 of 0.19) ends through `line_head`
            if line == "end" and wire is not None:
                g = stack.pop()
                if wire == "protobuf":
                    _check_scalar_body(g, seen.get("format"), "%s:%d" % (source, lineno), deferred)
                continue
            f = field_memo[1].get(line)
            if f is not None:
                stack[-1].lines.append(f)
                continue
            proto = group_memo[1].get(line)
        else:
            f = field_memo[0].get(line)
            if f is not None:
                (current.lines if current is not None else common).append(f)
                continue
            proto = group_memo[0].get(line)
        if proto is not None and not gstack:
            g = _open_group(
                proto, lineno, stack, common_lists, kind_lists, seen.get("format"), "%s:%d" % (source, lineno),
                deferred,
            )
            (stack[-1].lines if stack else current.lines if current is not None else common).append(g)
            stack.append(g)
            continue
        head = line_head(raw, source, lineno, seen)
        if head is None:
            continue
        kw, args = head
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        if kw == "wire":
            wire = args[0]
        if kw in ("tag", "key") or (kw == "checksum" and len(args) == 2) or (kw == "length" and wire == "tlv"):
            # a line of the tlv wire (0.13): its form, its place and its wire
            # are checked after the file is read
            tlv_lines.append((kw, args, lineno, current is not None, bool(stack)))
            continue
        if kw in ("ext", "choice", "alt") or (
                wire == "bits" and kw == "group" and args[1] == "size"):
            # 0.19 rulings 9 and 21: a line of a UPER pack, which a pack read here
            # (not a sequential `bits` pack) is not: `bad-directive` after the file
            # is read, on every wire for `ext`, `choice` and `alt`, on `bits` for
            # `group ... size`. Its block is still opened, so that its `end`
            # closes it; its lines are read, line checks included, and kept nowhere.
            deferred.append((lineno, BadDirective(
                "%r is a line of a UPER pack ('layout sequential', 'encoding uper'), at %s" % (kw, where),
                format=fmt,
            )))
            if kw != "alt":
                if gstack:
                    # inside a 0.17 gate block the block it opens nests in the
                    # gate, so that its `end` closes it and not the gate
                    gstack.append(Gate("", (0, 0, 1), (0,), False, gstack[-1], lineno))
                else:
                    stack.append(Group(args[0] if args else "", kw, (), lineno))
            continue
        try:
            if kw in _SEQ_ONLY:
                raise _Deferred(BadDirective(
                    "%r is a line of a sequential pack ('layout sequential' on the bits wire), at %s" % (kw, where),
                    format=fmt,
                ))
            if gstack and kw not in _GATE_LINES:
                # 0.17: only `field`, `const`, `default`, a nested `if` and `end` inside a gate block
                raise _Deferred(BadDirective("%r inside an 'if' block, at %s" % (kw, where), format=fmt))
            if stack and kw not in _GROUP_LINES:
                # 0.7: only `field`, `const`, `default`, `group` and `end` inside a group (rulings 9 and 18)
                raise _Deferred(BadDirective("%r inside a group, at %s" % (kw, where), format=fmt))
            if kw in HEADER_ONCE:
                pass  # read by `line_head`
            elif kw in _SINGLE:
                if kw in once:
                    raise BadDirective("a second %r line at %s" % (kw, where), format=fmt)
                once[kw] = (args[0], lineno)
                if kw == "layout":
                    if args[0] not in LAYOUTS:
                        raise BadDirective(
                            "layout must be one of %s, got %r at %s" % (", ".join(LAYOUTS), args[0], where),
                            format=fmt, locator="layout",
                        )
                    if wire != "bits":  # ruling 14 of 0.8
                        raise _Deferred(
                            BadDirective("'layout' is bits only, at %s" % where, format=fmt, locator="layout")
                        )
                elif kw == "bit_order":
                    if args[0] not in BIT_ORDERS:
                        raise BadDirective(
                            "bit_order must be one of %s, got %r at %s" % (", ".join(BIT_ORDERS), args[0], where),
                            format=fmt, locator="bit_order",
                        )
                    if wire != "bits":
                        raise _Deferred(
                            BadDirective("'bit_order' is bits only, at %s" % where, format=fmt, locator="bit_order")
                        )
                elif wire != "bits":  # word_bytes
                    raise _Deferred(
                        BadDirective("'word_bytes' is bits only, at %s" % where, format=fmt, locator="word_bytes")
                    )
                elif not (args[0].isascii() and args[0].isdigit()) or int(args[0]) < 1:
                    raise _Deferred(BadDirective(
                        "word_bytes is an unsigned decimal integer of 1 or more, got %r at %s" % (args[0], where),
                        format=fmt, locator="word_bytes",
                    ))
            elif kw in TEXT_DIRECTIVES:
                _text_directive(kw, args[0], text_dirs, current, fmt, lineno, where)
            elif kw == "armor":
                armor_line(args, armor, current is not None, lineno)
            elif kw == "encoding":  # 0.19: checked after the file is read (`check_encoding`)
                encoding.append((args[0], lineno, current is not None))
            elif kw == "root":
                if current is None:
                    if root is not None:
                        raise BadDirective("a second 'root' line at %s" % where, format=fmt)
                    root = args[0]
                    if wire != "xml":
                        raise _Deferred(BadDirective("'root' is xml only, at %s" % where, format=fmt, locator="root"))
                else:
                    if current.root is not None:
                        raise BadDirective(
                            "kind %r has two 'root' lines, at %s" % (current.name, where), format=fmt
                        )
                    current.root = args[0]
                    if wire != "xml":
                        raise _Deferred(BadDirective(
                            "'root' inside a kind is xml only, at %s" % where, format=fmt, locator="root"
                        ))
            elif kw == "kind":
                if args[0] in kind_names:
                    raise DuplicateKind("kind %r is declared twice, at %s" % (args[0], where), format=fmt)
                kind_names.add(args[0])
                current = Kind(args[0], lineno=lineno)
                kinds.append(current)
                kind_lists = set()
            elif kw == "group":
                if wire == "tlv":  # 0.13: `group` on `tlv` is `bad-directive`
                    deferred.append((lineno, BadDirective(
                        "'group' on the tlv wire, at %s" % where, format=fmt, locator=args[1]
                    )))
                    proto = Group(args[0], args[1], (), lineno)
                else:
                    n_before = len(deferred)
                    proto = _resolve_group(wire, fmt, args[0], args[1], args[2:], source, lineno, bool(stack),
                                           deferred)
                    if len(deferred) == n_before:
                        group_memo[1 if stack else 0][line] = proto
                g = _open_group(proto, lineno, stack, common_lists, kind_lists, fmt, where, deferred)
                (stack[-1].lines if stack else current.lines if current is not None else common).append(g)
                if args[0] not in group_refs:
                    group_refs.add(args[0])
                    key_refs.append((args[0], g))
                stack.append(g)
            elif kw == "if":
                # 0.17: every error of a gate line is `bad-directive`, after the
                # file. A gate that fails still opens its block, so that its
                # `end` closes the block and not an enclosing group.
                parent = gstack[-1] if gstack else None
                try:
                    if wire != "bits":
                        raise BadDirective("a gate block ('if') is bits only, at %s" % where, format=fmt)
                    gate = _resolve_gate(fmt, args, parent, source, lineno)
                except BadDirective as e:
                    deferred.append((lineno, e))
                    gate = Gate(args[0], (0, 0, 1), (0,), False, parent, lineno)
                (stack[-1].gates if stack else current.gates if current is not None else common_gates).append(gate)
                gstack.append(gate)
            elif kw == "end" and gstack:
                gstack.pop()
            elif kw == "end":
                if not stack:
                    raise _Deferred(BadDirective("'end' with no open group, at %s" % where, format=fmt))
                g = stack.pop()
                if wire == "protobuf":
                    _check_scalar_body(g, fmt, where, deferred)
            elif kw == "field":
                key, locator, tname = args[:3]
                f = _resolve_field(wire, fmt, key, locator, tname, args[3:], source, lineno, deferred,
                                   in_group=bool(stack))
                if f is None:
                    continue
                if gstack:
                    f.gate = gstack[-1]
                else:
                    field_memo[1 if stack else 0][line] = f
                (stack[-1].lines if stack else current.lines if current is not None else common).append(f)
                key_refs.append((key, f))
                if f.with_key is not None:  # checked as a `requires` key (0.18)
                    key_refs.append((f.with_key, None))
                if f.via is not None:
                    via_fields.append(f)
            elif kw == "element":  # 0.18; its wire is checked after the file
                try:
                    loc = parse_xml_locator(args[0], allow_presence=True)
                    if loc[1] != "elem" or any("@" in seg or "#" in seg for seg in loc[0]):
                        raise ValueError("a path of element names, without '@' or '#' (ruling 16)")
                except ValueError as e:
                    raise BadDirective("'element' path %r at %s: %s" % (args[0], where, e), format=fmt,
                                       locator=args[0])
                el = Element(args[0], loc[0], lineno)
                (current.lines if current is not None else common).append(el)
                elements.append((el, current))
            elif kw == "when" or kw == "const":
                if kw == "when" and first_when is None:
                    first_when = (lineno, args[0])
                w = _resolve_when(wire, fmt, args[0], args[1], source, lineno, deferred, directive=kw,
                                  in_group=bool(stack))
                if w is not None:
                    if gstack:
                        w.gate = gstack[-1]
                    (stack[-1].lines if stack else current.lines if current is not None else common).append(w)
            elif kw == "default":
                d = Default(args[0], args[1], lineno)
                if gstack:
                    gstack[-1].defaults[args[0]] = d
                elif stack:
                    stack[-1].defaults = list(stack[-1].defaults) + [d]
                else:
                    (current.defaults if current is not None else defaults).append(d)
                key_refs.append((args[0], d))
            elif kw == "map":
                tables.setdefault(args[0], []).append((args[1], args[2]))
            elif kw == "requires":
                if current is None:
                    raise _Deferred(BadDirective("'requires' before the first 'kind', at %s" % where, format=fmt))
                for term in args:
                    key, values = requires_term(term, lambda m: BadDirective("%s, at %s" % (m, where), format=fmt))
                    if key not in current.requires:
                        current.requires.append(key)
                    if values is not None:
                        vc = ValueCheck("requires", key, values, lineno, current)
                        current.requires_eq.append(vc)
                        value_checks.append(vc)
                    key_refs.append((key, None))
            elif kw == "source":
                sources.append(args[0])
            elif kw == "uses":
                if args[0] not in uses:
                    uses.append(args[0])
            else:
                typemap.append((lineno, args))
        except _Deferred as d:
            deferred.append((lineno, d.err))
        except _LineBad:
            raise
        except (BadDirective, DuplicateKind) as e:  # not a line check: after the file (ruling 5b of 0.11)
            deferred.append((lineno, e))

    name = seen.get("format")
    version = seen.get("version")
    if not name:
        raise MissingFormat("the pack has no 'format' line in %s" % source)
    if not version:
        raise MissingVersion("the pack has no 'version' line in %s" % source, format=name)
    if wire is None:
        raise MissingWire("the pack has no 'wire' line in %s" % source, format=name)
    if gstack:
        deferred.append((gstack[-1].lineno, BadDirective(
            "'if' with no 'end', at %s:%d" % (source, gstack[-1].lineno), format=name, locator=gstack[-1].locator,
        )))
    if stack:
        deferred.append((stack[-1].lineno, BadDirective(
            "'group' with no 'end', at %s:%d" % (source, stack[-1].lineno), format=name, locator=stack[-1].locator,
        )))
    word_bytes_raw = once.get("word_bytes", (None,))[0]
    if wire == "bits":
        _check_bits_groups(word_bytes_raw, common, kinds, deferred, source, name, common_gates)
    elif group_refs and wire in ("xml", "json", "protobuf", "geojson"):
        _check_group_paths(wire, common, kinds, deferred, source, name)
    armor_value = check_armor(armor, wire, once.get("bit_order", (None,))[0], deferred, source, name)
    check_encoding(encoding, wire, False, once.get("bit_order", (None,))[0], armor_value, deferred, source, name)
    text_values = {k: v for k, (v, _) in text_dirs.items()}
    delimiter = text_values.get("delimiter", ",")
    if delimiter == "none":
        delimiter = None
    if wire == "text":
        wire_text.check_pack(
            delimiter, text_values.get("start"), text_values.get("checksum", "none"), common, kinds,
            {k: ln for k, (_, ln) in text_dirs.items()}, deferred, source, name,
        )
    else:
        for kw, (_, ln) in text_dirs.items():  # ruling 10 of 0.10, wherever the `wire` line stands
            deferred.append((ln, BadDirective(
                "%r is a directive of the text wire, at %s:%d" % (kw, source, ln), format=name, locator=kw
            )))
    tlv = _check_tlv_lines(tlv_lines, wire, deferred, source, name)
    if wire == "tlv":
        wire_tlv.check_pack(
            tlv.get("tag", ("ber-oid",))[0], tlv.get("checksum", (None,))[0], tlv.get("checksum", (None, 0))[1],
            common, kinds, deferred, source, name,
        )
    for el, k in elements:  # 0.18: `element` is xml only; ruling 12
        if wire != "xml":
            deferred.append((el.lineno, BadDirective(
                "'element' is xml only, at %s:%d" % (source, el.lineno), format=name, locator=el.locator,
            )))
            continue
        groups = [ln for ln in common if ln.is_group]
        for kk in kinds if k is None else (k,):
            groups.extend(ln for ln in kk.lines if ln.is_group)
        for g in groups:
            gp = tuple(g.loc)
            if el.loc[:len(gp)] == gp:
                deferred.append((el.lineno, BadDirective(
                    "'element' %s equals or passes through the path of group %s, at %s:%d"
                    % (el.locator, g.list, source, el.lineno), format=name, locator=el.locator,
                )))
                break
    if first_when is not None and not kinds:
        deferred.append((first_when[0], BadDirective(
            "'when' in a pack with no 'kind' line, at %s:%d" % (source, first_when[0]),
            format=name, locator=first_when[1],
        )))
    for f in via_fields:
        if f.via not in tables:
            deferred.append((f.lineno, BadDirective(
                "'via' names table %r, which has no 'map' line, at %s:%d" % (f.via, source, f.lineno),
                format=name, locator=f.locator,
            )))
    if deferred:
        raise min(deferred, key=lambda d: d[0])[1]
    if wire == "xml" and not root and not (kinds and all(k.root for k in kinds)):
        # A pack whose every kind has a `root` needs no pack-level `root` (0.6).
        raise MissingRoot("xml pack needs a 'root' directive", format=name, locator="root")
    word_bytes = None
    if wire == "bits":
        if word_bytes_raw is None:
            raise BadDirective("a fixed-layout bits pack needs a 'word_bytes' line", format=name, locator="word_bytes")
        word_bytes = int(word_bytes_raw)
    distribution = seen.get("distribution")
    if distribution is None:
        raise MissingDistribution("pack has no 'distribution' line in %s" % source, format=name)
    if not sources:
        raise MissingSource("pack has no 'source' line in %s" % source, format=name)

    pack = Pack(
        name=name, version=version, wire=wire, root=root, word_bytes=word_bytes,
        bit_order=once.get("bit_order", ("lsb",))[0], common=common, kinds=kinds, key_refs=key_refs,
        distribution=distribution, sources=sources, uses=uses, defaults=defaults, tables=tables,
        delimiter=delimiter, start=text_values.get("start"), checksum=text_values.get("checksum", "none"),
        armor=armor_value,
        gates=common_gates, value_checks=value_checks,
    )
    if wire == "tlv":
        pack.tlv_tag = tlv.get("tag", ("ber-oid",))[0]
        pack.tlv_length = tlv.get("length", ("ber",))[0]
        pack.tlv_key = tlv.get("key", (None,))[0]
        pack.tlv_checksum = tlv.get("checksum", (None,))[0]
    for lineno, (native, cot) in typemap:
        if native in pack.native_to_cot or cot in pack.cot_to_native:
            raise BadDirective(
                "typemap %r <-> %r is not one-to-one at %s:%d" % (native, cot, source, lineno), format=name
            )
        pack.native_to_cot[native] = cot
        pack.cot_to_native[cot] = native
    set_bit_order(pack)
    return pack


def set_bit_order(pack):
    """Give each transform that reads its field in the pack's bit order
    (`chars`, 0.15) that order."""
    msb = pack.bit_order == "msb"
    for _, ref in pack.key_refs:
        t = getattr(ref, "transform", None)
        if getattr(t, "bit_ordered", False):
            t.msb = msb


def load_pack(directory):
    path = os.path.join(directory, "pack.def")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    pack = parse_pack_text(text, source=path)
    pack.path = directory
    return pack
