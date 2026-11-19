"""pack.def parsing (grammar: spec/engine-spec.md, "Pack definition grammar")."""

import os
import re
from dataclasses import dataclass, field as dc_field

from .errors import (
    BadDirective, DuplicateKind, InvalidPack, MissingDistribution, MissingRoot, MissingSource,
    UnknownDirective, UnknownTransform,
)
from .transforms import (
    TRANSFORMS, TEXT_TRANSFORM_NAMES, JSON_TRANSFORMS, build_bits_transform, bits_len_allowed,
    is_bits_transform_name, PROTOBUF_TRANSFORMS, is_protobuf_transform_name,
)

WIRES = ("xml", "geojson", "json", "bits", "protobuf")
BIT_ORDERS = ("lsb", "msb")
DISTRIBUTIONS = ("public", "controlled")  # public is below controlled
CORE = "core"
_ARITY = {
    "format": 1, "version": 1, "wire": 1, "root": 1, "word_bytes": 1, "typemap": 2, "layout": 1,
    "distribution": 1, "source": 1, "uses": 1,
    "kind": 1, "when": 2, "bit_order": 1,
    "const": 2, "default": 2, "map": 3, "end": 0,
    # `field` takes three or more, `requires` one or more, `group` two or more
    "field": None, "requires": None, "group": None,
}
_SINGLE = ("format", "version", "wire", "word_bytes", "distribution", "bit_order", "layout")
LAYOUTS = ("fixed", "sequential")
# Lines of a sequential pack (0.8): each is `bad-directive` in a fixed-layout pack.
_SEQ_ONLY = ("pad", "align", "presence", "if", "opt", "chain", "fx", "length", "also")
_GROUP_LINES = ("field", "const", "default", "group", "end")
FIELD_OPTIONS = ("via", "swap", "trim", "optional")


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

    is_field = False
    is_const = False
    is_group = False


@dataclass
class Const:
    """`const <locator> <value>` (0.6): written on encode as a `when` value is,
    not checked on decode; the member it names is mapped."""

    locator: str
    loc: tuple
    value: object = None
    forced: bool = False
    lineno: int = 0

    is_field = False
    is_const = True
    is_group = False
    present = False


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

    @property
    def fields(self):
        return [ln for ln in self.lines if ln.is_field]

    @property
    def whens(self):
        return [ln for ln in self.lines if not ln.is_field and not ln.is_const and not ln.is_group]


class Group:
    """`group <list> <locator> ... end` (0.7): the part of a message that
    repeats, mapped to a list of the model. `lines` and `defaults` are the body,
    every locator of which is read relative to one item. On `bits`, `loc` is
    `(word, pos)` of the first item, `item_bits` the nominal size of an item,
    and the count is `count_loc` (a `<word>:<pos>:<len>` of the enclosing
    scope) or the fixed `count_n`."""

    __slots__ = ("list", "locator", "loc", "lineno", "lines", "defaults", "item_bits", "count_loc", "count_n",
                 "_plan", "_seen")

    is_field = False
    is_const = False
    is_group = True
    present = False

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

    @property
    def plan(self):
        """The body as a scope: what applies to one item."""
        plan = self._plan
        if plan is None:
            plan = self._plan = Plan(self.lines, self.defaults)
        return plan


class Plan:
    """What applies to one scope: a message of one kind (or of a pack with no
    kinds), or one item of a group. The lines in line order (common first);
    the `when` lines among them; the `field` and `group` lines with their
    index; the defaults in scope; each line's locator once, in line order; and
    the locators that are mapped, so are not residue (every `field`, `const`
    and `group`, every `when` with a value; `when <locator> present` does not
    map its member, 0.5 ruling 27)."""

    __slots__ = ("lines", "whens", "defaults", "order", "mapped", "root", "cache", "fields", "groups",
                 "ifields", "igroups")

    def __init__(self, lines, defaults=(), root=None):
        self.lines = lines
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
            ]
            return value
        if name == "order" or name == "mapped":
            order = self.order = []
            mapped = self.mapped = set()
            for ln in self.lines:
                if not ln.is_field and ln.present:
                    continue
                if ln.locator not in mapped:
                    mapped.add(ln.locator)
                    order.append(ln.locator)
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
                plan = Plan(self.common, self.defaults, self.root)
            else:
                plan = Plan(self.common + kind.lines, self.defaults + kind.defaults, kind.root or self.root)
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
_TOKEN = re.compile(r'''\s*(?:"([^"]*)"|(["#])|(\S+))''')


def tokenize(line, *, source="pack.def", lineno=0, error=InvalidPack):
    """Split one line into tokens. `#` starts a comment only at the start of a
    token (so `path/#text` is a locator, not a comment). Double quotes wrap a
    token containing whitespace; inside the quotes `#` is literal and there is
    no escape, so a quoted value cannot contain a double quote (0.5 rulings 6, 17)."""
    if '"' not in line:
        # No quoted token: whitespace-separated tokens up to the first that starts with `#`.
        toks = line.split()
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
    try:
        word, pos, length = (int(p) for p in parts)
    except ValueError:
        raise ValueError("bits locator fields must be integers")
    if word < 0:
        raise ValueError("word index must be >= 0")
    if pos < 0:
        raise ValueError("pos must be >= 0")
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
        if not part.isdigit():
            raise ValueError("protobuf locator must be dot-separated field numbers: %r" % locator)
        num = int(part)
        if num < 1:
            raise ValueError("protobuf field number must be >= 1")
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
    return parse_geojson_locator(locator)


class _Deferred(Exception):
    """A `bad-directive` that needs the whole file: it is raised after the file
    is read and before `missing-distribution` (0.5 ruling 25)."""

    def __init__(self, err):
        Exception.__init__(self, str(err))
        self.err = err


def _parse_options(tokens, fmt, locator, where):
    """-> {option: value} for the tokens after a `field` line's transform."""
    out = {}
    i = 0
    while i < len(tokens):
        opt = tokens[i]
        if opt not in FIELD_OPTIONS:  # 0.6 ruling 12
            raise BadDirective("unknown field option %r at %s" % (opt, where), format=fmt, locator=locator)
        if opt in out:
            raise BadDirective("field option %r is given twice at %s" % (opt, where), format=fmt, locator=locator)
        if opt == "via":
            if i + 1 >= len(tokens):
                raise BadDirective("field option 'via' takes a table name at %s" % where, format=fmt, locator=locator)
            out[opt] = tokens[i + 1]
            i += 2
        else:
            out[opt] = True
            i += 1
    return out


def _item_locator(wire, name, locator, in_group, where):
    """`.` addresses the item itself when it is a scalar (`json`, `geojson`,
    `protobuf`, inside a group): -> True when `locator` is that. Outside a
    group it is `bad-directive`."""
    if locator != "." or wire not in ("json", "geojson", "protobuf"):
        return False
    if not in_group:
        raise BadDirective(
            "the locator '.' addresses a scalar item and is written inside a group, at %s" % where,
            format=name, locator=locator,
        )
    return True


def _resolve_field(wire, name, key, locator, tname, options, source, lineno, in_group=False):
    """-> Field for one field line, enforcing that a transform is used only on
    a wire it is valid on, and an option only on a wire and transform it
    applies to (spec/engine-spec.md). Inside a group the locator is relative
    to one item (0.7)."""
    where = "%s:%d" % (source, lineno)
    opts = _parse_options(options, name, locator, where)
    via = opts.get("via")

    def misplaced(opt, why):
        return _Deferred(BadDirective(
            "field option %r %s at %s" % (opt, why, where), format=name, locator=locator
        ))

    if wire == "bits":
        if tname in TEXT_TRANSFORM_NAMES or is_protobuf_transform_name(tname):
            raise InvalidPack(
                "transform %r is not valid on a bits wire" % tname,
                format=name, locator=locator,
            )
        if locator.isascii() and locator.isdigit():
            raise BadDirective(
                "a field with a bit length for a locator is a line of a sequential pack, at %s" % where,
                format=name, locator=locator,
            )
        try:
            loc = parse_bits_locator(locator)
        except ValueError as e:
            raise InvalidPack(
                "bad bits locator at %s: %s" % (where, e), format=name, locator=locator
            )
        if tname in ("f32", "f64", "ascii"):
            if not bits_len_allowed(tname, loc[2]):
                raise _Deferred(BadDirective(
                    "transform %s does not allow a bit length of %d at %s" % (tname, loc[2], where),
                    format=name, locator=locator,
                ))
        elif not 1 <= loc[2] <= 64:
            raise InvalidPack(
                "bad bits locator at %s: len must be in 1..=64" % where, format=name, locator=locator
            )
        try:
            transform = build_bits_transform(tname, loc[2])
        except ValueError as e:
            raise UnknownTransform(
                "unknown transform %r at %s: %s" % (tname, where, e),
                format=name, locator=locator,
            )
        if "swap" in opts and loc[2] % 8 != 0:
            raise misplaced("swap", "needs a bit length that is a multiple of 8, got %d," % loc[2])
        if "trim" in opts:
            if tname != "ascii":
                raise misplaced("trim", "applies to the 'ascii' transform only")
            transform.trim = True
        return Field(key, locator, transform, loc, lineno, via=via, swap="swap" in opts, optional="optional" in opts)

    for opt in ("swap", "trim", "optional"):
        if opt in opts:
            raise misplaced(opt, "applies to the bits wire only")

    if wire == "protobuf":
        if tname in TEXT_TRANSFORM_NAMES or is_bits_transform_name(tname):
            raise InvalidPack(
                "transform %r is not valid on a protobuf wire" % tname,
                format=name, locator=locator,
            )
        transform = PROTOBUF_TRANSFORMS.get(tname)
        if transform is None:
            raise UnknownTransform(
                "unknown transform %r at %s" % (tname, where),
                format=name, locator=locator,
            )
        try:
            loc = () if _item_locator(wire, name, locator, in_group, where) else parse_protobuf_locator(locator)
        except ValueError as e:
            raise InvalidPack(
                "bad protobuf locator at %s: %s" % (where, e),
                format=name, locator=locator,
            )
        return Field(key, locator, transform, loc, lineno, via=via)

    if is_bits_transform_name(tname) or is_protobuf_transform_name(tname) or (tname == "bool" and wire != "json"):
        raise InvalidPack(
            "transform %r is not valid on a %s wire" % (tname, wire),
            format=name, locator=locator,
        )
    transform = (JSON_TRANSFORMS if wire == "json" else TRANSFORMS).get(tname)
    if transform is None:
        raise UnknownTransform(
            "unknown transform %r at %s" % (tname, where), format=name, locator=locator
        )
    try:
        if _item_locator(wire, name, locator, in_group, where):
            loc = ()
        elif in_group and wire == "geojson":
            loc = parse_json_locator(locator)  # an item of a geojson list is addressed as on `json`
        else:
            loc = parse_locator(wire, locator)
    except ValueError as e:
        raise InvalidPack(
            "bad %s locator at %s: %s" % (wire, where, e), format=name, locator=locator
        )
    return Field(key, locator, transform, loc, lineno, via=via)


def _resolve_group(wire, name, list_name, locator, options, source, lineno, in_group):
    """-> Group for one `group` line (spec 0.7, "Group locators and
    item-relative locators, by wire")."""
    where = "%s:%d" % (source, lineno)

    def bad(msg):
        return BadDirective("%s at %s" % (msg, where), format=name, locator=locator)

    if wire == "bits" and locator in ("count", "while", "more", "fx"):
        raise bad("a group with %r is a line of a sequential pack" % locator)
    opts = {}
    i = 0
    while i < len(options):
        opt = options[i]
        if opt not in ("item", "count") or opt in opts or i + 1 >= len(options):
            raise bad("a 'group' line is: group <list> <locator> [item <bits>] [count <locator | n>]; got %r" % opt)
        opts[opt] = options[i + 1]
        i += 2
    if wire != "bits":
        for opt in ("count", "item"):
            if opt in opts:
                raise bad("%r on a 'group' is bits only" % opt)
        try:
            if wire == "xml":
                loc = parse_xml_locator(locator, allow_presence=True)
                if loc[1] != "elem":
                    raise ValueError("an xml group locator is a path of element names")
                loc = loc[0]
            elif wire == "protobuf":
                loc = parse_protobuf_locator(locator)
            elif wire == "json" or in_group:
                loc = parse_json_locator(locator)
            else:
                loc = parse_geojson_locator(locator)
                if loc[0] == "coord":
                    raise ValueError("a geojson group locator is a properties member or a top-level member")
        except ValueError as e:
            raise InvalidPack("bad %s group locator at %s: %s" % (wire, where, e), format=name, locator=locator)
        return Group(list_name, locator, loc, lineno)

    parts = locator.split(":")
    if len(parts) != 2 or not all(p.isascii() and p.isdigit() for p in parts):
        raise InvalidPack("bad bits group locator at %s: it is <word>:<pos>" % where, format=name, locator=locator)
    if "item" not in opts:
        raise bad("a bits group needs 'item <bits>'")
    if "count" not in opts:
        raise bad("a bits group needs 'count <count-locator | n>'")
    item = opts["item"]
    if not (item.isascii() and item.isdigit()) or int(item) < 1:
        raise bad("'item' takes a number of bits, got %r" % item)
    g = Group(list_name, locator, (int(parts[0]), int(parts[1])), lineno, item_bits=int(item))
    count = opts["count"]
    if count.isascii() and count.isdigit():
        g.count_n = int(count)
    else:
        try:
            g.count_loc = parse_bits_locator(count)
        except ValueError as e:
            raise bad("'count' takes a number or a <word>:<pos>:<len> locator (%s)" % e)
        if g.count_loc[2] > 64:
            raise bad("a count locator is at most 64 bits")
    return g


def _open_group(proto, lineno, stack, common_lists, kind_lists, fmt, where):
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
    if twice:
        raise BadDirective(
            "list %s is mapped by two groups of one scope, at %s" % (name, where), format=fmt, locator=proto.locator
        )
    seen.add(name)
    return Group(name, proto.locator, proto.loc, lineno, proto.item_bits, proto.count_loc, proto.count_n)


def _resolve_when(wire, name, locator, value, source, lineno, directive="when", in_group=False):
    """-> When for one `when` line (spec 0.5, "`when` values, by wire"), or
    Const for one `const` line, whose value is typed as a `when` value is and
    is never `present` (spec 0.6, "Constants")."""
    where = "%s:%d" % (source, lineno)
    const = directive == "const"

    def make(val, forced=False):
        if const:
            return Const(locator, loc, value=val, forced=forced, lineno=lineno)
        return When(locator, loc, value=val, forced=forced, lineno=lineno)

    if locator == "." and wire in ("json", "geojson", "protobuf"):
        raise BadDirective(
            "the locator '.' is for a 'field' of a scalar item, not a '%s', at %s" % (directive, where),
            format=name, locator=locator,
        )
    try:
        loc = parse_json_locator(locator) if in_group and wire == "geojson" else parse_locator(wire, locator)
    except ValueError as e:
        raise InvalidPack("bad %s locator at %s: %s" % (wire, where, e), format=name, locator=locator)
    if const and value == "present":
        raise BadDirective(
            "a 'const' value is written, so cannot be 'present' (text wires: '=present'), at %s" % where,
            format=name, locator=locator,
        )
    if wire == "bits":
        if not 1 <= loc[2] <= 64:
            raise InvalidPack("bad bits locator at %s: len must be in 1..=64" % where, format=name, locator=locator)
        if not (value.isascii() and value.isdigit()):
            raise BadDirective(
                "a bits '%s' value is an unsigned decimal integer, got %r at %s" % (directive, value, where),
                format=name, locator=locator,
            )
        n = int(value)
        if n >= (1 << loc[2]):
            raise _Deferred(BadDirective(
                "'%s' value %d does not fit %d bit(s) at %s" % (directive, n, loc[2], where),
                format=name, locator=locator,
            ))
        return make(n)
    if wire == "protobuf":
        if value == "present":
            return When(locator, loc, present=True, lineno=lineno)
        if not (value.isascii() and value.isdigit()):
            raise BadDirective(
                "a protobuf '%s' value is %san unsigned decimal integer, got %r at %s"
                % (directive, "" if const else "'present' or ", value, where),
                format=name, locator=locator,
            )
        n = int(value)
        if n >= (1 << 64):
            raise _Deferred(BadDirective(
                "'%s' value %d does not fit a varint at %s" % (directive, n, where), format=name, locator=locator
            ))
        return make(n)
    if value == "present":
        return When(locator, loc, present=True, lineno=lineno)
    if value.startswith("="):
        return make(value[1:], forced=True)
    return make(value)


def _check_bits_groups(word_bytes, common, kinds, deferred, source, name):
    """The checks of a `bits` pack's groups that need the whole file (0.7):
    a `field`, `when`, `const` or second group whose bits lie at or beyond the
    start of a group counted by a locator; a count locator that reaches its
    group's start; a field of an item, or a nested group with a fixed count,
    that reaches beyond `item <bits>`. Each is `bad-directive`."""
    if word_bytes is None or not (word_bytes.isascii() and word_bytes.isdigit()) or int(word_bytes) < 1:
        return  # reported as an invalid pack after the whole-file checks
    word_bits = int(word_bytes) * 8

    def bad(ln, msg):
        deferred.append((ln.lineno, BadDirective(
            "%s, at %s:%d" % (msg, source, ln.lineno), format=name, locator=ln.locator
        )))

    def start(loc):
        return loc[0] * word_bits + loc[1]

    def scope(lines, item_bits):
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

    def bodies(lines):
        for ln in lines:
            if ln.is_group:
                scope(ln.lines, ln.item_bits)
                bodies(ln.lines)

    def has_counted(lines):
        return any(ln.is_group and ln.count_loc is not None for ln in lines)

    bodies(common)
    for k in kinds:
        bodies(k.lines)
    common_counted = has_counted(common)
    if common_counted:
        scope(common, None)
    for k in kinds:  # a scope is common plus one kind
        if common_counted or has_counted(k.lines):
            scope(common + k.lines, None)


def _find_wire(text, directive="wire"):
    """The value of the first `wire` line, so that `field` and `when` lines are
    checked as the file is read wherever the `wire` line stands."""
    for raw in text.splitlines():
        if directive not in raw:
            continue
        try:
            toks = tokenize(raw)
        except InvalidPack:
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


def _check_scalar_body(g, fmt, where):
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
        raise BadDirective(
            "the body of group %s mixes '.' with paths, at %s" % (g.list, where), format=fmt, locator=g.locator
        )


def parse_pack_text(text, *, source="pack.def"):
    """Parse one pack.def. Line errors are raised as the file is read; then the
    checks that need the whole file (`bad-directive`: `when` with no `kind`,
    `bit_order` or a kind's `root` on another wire, a length a transform does
    not allow, a `when` or `const` value that does not fit, a field option
    where it does not apply, `via` naming a table with no `map` line), the
    first in line order; then `missing-distribution`, `missing-source`."""
    wire = _find_wire(text)
    wire_ok = wire in WIRES
    if wire == "bits" and _find_wire(text, "layout") == "sequential":
        from .seqdef import parse_seq_pack_text
        return parse_seq_pack_text(text, source=source)
    seen = {}
    typemap = []
    sources = []
    uses = []
    common = []
    kinds = []
    kind_names = set()
    key_refs = []
    defaults = []
    tables = {}
    via_fields = []
    deferred = []        # (lineno, BadDirective)
    root = None
    current = None       # the Kind being read, None before the first `kind`
    first_when = None    # (lineno, locator) of the first `when`, for the no-kind check
    stack = []           # the open groups, outermost first (0.7)
    common_lists = set() # lists mapped by a common group
    kind_lists = None    # lists mapped by a group of the kind being read
    # A generated pack repeats a few thousand distinct lines over a million
    # times: a `field` or `group` line is resolved once per distinct text (and
    # per position, inside a group or not). Lines of one text share one Field.
    field_memo = ({}, {})
    group_memo = ({}, {})
    group_refs = set()   # list names already in key_refs
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if stack:
            if line == "end":
                g = stack.pop()
                if wire == "protobuf":
                    _check_scalar_body(g, seen.get("format"), "%s:%d" % (source, lineno))
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
        if proto is not None:
            g = _open_group(
                proto, lineno, stack, common_lists, kind_lists, seen.get("format"), "%s:%d" % (source, lineno)
            )
            (stack[-1].lines if stack else current.lines if current is not None else common).append(g)
            stack.append(g)
            continue
        toks = tokenize(raw, source=source, lineno=lineno)
        if not toks:
            continue
        kw, args = toks[0], toks[1:]
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        if kw in _SEQ_ONLY:
            raise BadDirective(
                "%r is a line of a sequential pack ('layout sequential' on the bits wire), at %s" % (kw, where),
                format=fmt,
            )
        if kw not in _ARITY:
            raise UnknownDirective("unknown directive %r at %s" % (kw, where), format=fmt)
        if stack and kw not in _GROUP_LINES:
            # 0.7: only `field`, `const`, `default`, `group` and `end` inside a group (rulings 9 and 18)
            raise BadDirective("%r inside a group, at %s" % (kw, where), format=fmt)
        if kw == "requires":
            if not args:
                raise InvalidPack("directive 'requires' takes 1 or more argument(s), got 0 at %s" % where, format=fmt)
        elif kw == "group":
            if len(args) < 2:
                raise InvalidPack(
                    "directive 'group' takes 2 or more argument(s), got %d at %s" % (len(args), where), format=fmt
                )
        elif kw == "field":
            if len(args) < 3:
                raise InvalidPack(
                    "directive 'field' takes 3 or more argument(s), got %d at %s" % (len(args), where), format=fmt
                )
        elif len(args) != _ARITY[kw]:
            raise InvalidPack(
                "directive %r takes %d argument(s), got %d at %s" % (kw, _ARITY[kw], len(args), where),
                format=fmt,
            )
        try:
            if kw in _SINGLE:
                if kw in seen:
                    raise InvalidPack("duplicate directive %r at %s" % (kw, where), format=fmt)
                seen[kw] = args[0]
                if kw == "wire" and not wire_ok:
                    raise InvalidPack("unknown wire %r (known: %s)" % (wire, ", ".join(WIRES)), format=fmt)
                if kw == "layout":
                    if args[0] not in LAYOUTS:
                        raise BadDirective(
                            "layout must be one of %s, got %r at %s" % (", ".join(LAYOUTS), args[0], where),
                            format=fmt, locator="layout",
                        )
                    if wire_ok and wire != "bits":  # ruling 14 of 0.8
                        raise _Deferred(
                            BadDirective("'layout' is bits only, at %s" % where, format=fmt, locator="layout")
                        )
                if kw == "bit_order":
                    if args[0] not in BIT_ORDERS:
                        raise BadDirective(
                            "bit_order must be one of %s, got %r at %s" % (", ".join(BIT_ORDERS), args[0], where),
                            format=fmt, locator="bit_order",
                        )
                    if wire_ok and wire != "bits":
                        raise _Deferred(
                            BadDirective("'bit_order' is bits only, at %s" % where, format=fmt, locator="bit_order")
                        )
            elif kw == "root":
                if current is None:
                    if root is not None:
                        raise InvalidPack("duplicate directive 'root' at %s" % where, format=fmt)
                    root = args[0]
                else:
                    if current.root is not None:
                        raise InvalidPack(
                            "kind %r has two 'root' lines, at %s" % (current.name, where), format=fmt
                        )
                    current.root = args[0]
                    if wire_ok and wire != "xml":
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
                if wire_ok:
                    proto = _resolve_group(wire, fmt, args[0], args[1], args[2:], source, lineno, bool(stack))
                    group_memo[1 if stack else 0][line] = proto
                else:
                    proto = Group(args[0], args[1], (), lineno)
                g = _open_group(proto, lineno, stack, common_lists, kind_lists, fmt, where)
                (stack[-1].lines if stack else current.lines if current is not None else common).append(g)
                if args[0] not in group_refs:
                    group_refs.add(args[0])
                    key_refs.append((args[0], g))
                stack.append(g)
            elif kw == "end":
                if not stack:
                    raise BadDirective("'end' with no open group, at %s" % where, format=fmt)
                g = stack.pop()
                if wire == "protobuf":
                    _check_scalar_body(g, fmt, where)
            elif kw == "field":
                key, locator, tname = args[:3]
                if not wire_ok:
                    continue  # no wire to check against; the missing or unknown wire is reported instead
                f = _resolve_field(wire, fmt, key, locator, tname, args[3:], source, lineno, in_group=bool(stack))
                field_memo[1 if stack else 0][line] = f
                (stack[-1].lines if stack else current.lines if current is not None else common).append(f)
                key_refs.append((key, f))
                if f.via is not None:
                    via_fields.append(f)
            elif kw == "when" or kw == "const":
                if kw == "when" and first_when is None:
                    first_when = (lineno, args[0])
                if not wire_ok:
                    continue
                w = _resolve_when(wire, fmt, args[0], args[1], source, lineno, directive=kw, in_group=bool(stack))
                (stack[-1].lines if stack else current.lines if current is not None else common).append(w)
            elif kw == "default":
                d = Default(args[0], args[1], lineno)
                if stack:
                    stack[-1].defaults = list(stack[-1].defaults) + [d]
                else:
                    (current.defaults if current is not None else defaults).append(d)
                key_refs.append((args[0], d))
            elif kw == "map":
                tables.setdefault(args[0], []).append((args[1], args[2]))
            elif kw == "requires":
                if current is None:
                    raise BadDirective("'requires' before the first 'kind', at %s" % where, format=fmt)
                for key in args:
                    if key not in current.requires:
                        current.requires.append(key)
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

    name = seen.get("format")
    if stack:
        deferred.append((stack[-1].lineno, BadDirective(
            "'group' with no 'end', at %s:%d" % (source, stack[-1].lineno), format=name, locator=stack[-1].locator,
        )))
    if wire == "bits":
        _check_bits_groups(seen.get("word_bytes"), common, kinds, deferred, source, name)
    elif group_refs and wire in ("xml", "json", "protobuf", "geojson"):
        _check_group_paths(wire, common, kinds, deferred, source, name)
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
    distribution = seen.get("distribution")
    if distribution is None:
        raise MissingDistribution("pack has no 'distribution' line in %s" % source, format=name)
    if not sources:
        raise MissingSource("pack has no 'source' line in %s" % source, format=name)
    if distribution not in DISTRIBUTIONS:
        raise InvalidPack(
            "unknown distribution %r (known: %s)" % (distribution, ", ".join(DISTRIBUTIONS)), format=name
        )
    if not name:
        raise InvalidPack("missing 'format' directive in %s" % source)
    if not wire:
        raise InvalidPack("missing 'wire' directive in %s" % source, format=name)
    version = seen.get("version")
    if not version:
        raise InvalidPack("missing 'version' directive in %s" % source, format=name)
    if wire == "xml" and not root and not (kinds and all(k.root for k in kinds)):
        # A pack whose every kind has a `root` needs no pack-level `root` (0.6).
        raise MissingRoot("xml pack needs a 'root' directive", format=name, locator="root")
    if wire != "xml" and root:
        raise InvalidPack("'root' is xml only", format=name, locator="root")

    word_bytes_raw = seen.get("word_bytes")
    word_bytes = None
    if wire == "bits":
        if word_bytes_raw is None:
            raise InvalidPack("bits pack needs a 'word_bytes' directive", format=name, locator="word_bytes")
        try:
            word_bytes = int(word_bytes_raw)
        except ValueError:
            raise InvalidPack(
                "word_bytes must be an integer, got %r" % word_bytes_raw, format=name, locator="word_bytes"
            )
        if word_bytes < 1:
            raise InvalidPack("word_bytes must be >= 1", format=name, locator="word_bytes")
    elif word_bytes_raw is not None:
        raise InvalidPack("'word_bytes' is bits only", format=name, locator="word_bytes")

    pack = Pack(
        name=name, version=version, wire=wire, root=root, word_bytes=word_bytes,
        bit_order=seen.get("bit_order", "lsb"), common=common, kinds=kinds, key_refs=key_refs,
        distribution=distribution, sources=sources, uses=uses, defaults=defaults, tables=tables,
    )
    for lineno, (native, cot) in typemap:
        if native in pack.native_to_cot or cot in pack.cot_to_native:
            raise InvalidPack(
                "typemap %r <-> %r is not one-to-one at %s:%d" % (native, cot, source, lineno), format=name
            )
        pack.native_to_cot[native] = cot
        pack.cot_to_native[cot] = native
    return pack


def load_pack(directory):
    path = os.path.join(directory, "pack.def")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    pack = parse_pack_text(text, source=path)
    pack.path = directory
    return pack
