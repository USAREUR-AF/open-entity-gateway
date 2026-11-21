"""pack.def parsing for `layout sequential` (spec/engine-spec.md, "Version
0.8: sequential binary layouts"): a `bits` pack whose lines are read in order
from a cursor. A locator is a bit length, not a position.

The file becomes a tree of `Node`s. A `Body` is a scope: the message, or the
body of one group. It holds the lines before the first `kind` (common) and the
kinds; one item, like one message, is the common lines plus one kind.
"""

from .errors import (
    BadDirective, BadLocator, DuplicateKind, MissingDistribution, MissingFormat, MissingSource, MissingVersion,
)
from .transforms import (
    UntilCharsT, bits_len_allowed, build_bits_transform, read_int_text, read_uint_text, terminator_code,
    transform_valid_on, v15_family,
)
from . import uper

(T_FIELD, T_CONST, T_PAD, T_ALIGN, T_WHEN, T_PRESENCE, T_IF, T_OPT, T_GROUP, T_CHAIN, T_LENGTH,
 T_WHOLE, T_EXT, T_CHOICE, T_ALT, T_SWITCH, T_COUNT) = range(17)

# 0.19 adds `size`; 0.21 adds `rest`, `last` and `count @<name>` (the count of a `count` line)
GROUP_MODES = ("count", "while", "more", "fx", "size", "rest", "last")
_HEADER = ("format", "version", "wire", "layout", "bit_order", "word_bytes", "distribution", "source", "uses",
           "map", "typemap", "root", "armor", "encoding")
_SINGLE = ("layout", "bit_order")


class Node:
    """One line of a sequential pack, with the lines of its block.

    field     `bindings`: the `Field` of the line and of each `also` line after it; `length`
    const     `length`, `value`              when   `length`, `value`
    pad       `length`                       align  `length` (the multiple)
    presence  `name`, `length` (None with `fx`), `named`: the bits some `if` names
    if        `conds`: (presence node, bit, negated); `body`
    opt       `body`
    group     `list`, `mode`, `length` (the count's bits), `scope`: the Body of an item; with
              `count @<name>` (0.21) `count_node`, the `count` line; `tail_rest`: this `rest`
              group is the last line of an item of an outer `rest` group (ruling 10 of 0.21)
    count     `name`, `length`, `plus` (0.21)
    chain     `parts`: the lines between `fx` lines; `ends_fx`: the last line is `fx`
    length    `length`, `unit` ("bytes" | "bits"), `plus`, `body`
    whole     `length`, `plus`
    ext       `body` (0.19)
    choice    `n`, `ext`, `alts`: the `alt` nodes in line order, `index`: alt index -> node (0.19)
    alt       `value` (its index), `body`
    switch    `key`, `cases` (Case), `other` (the `else` Case or None), `key_type`, `key_lines`: the
              field nodes of the scope that map the key before the switch, in reading order (0.20)
    A field with a UPER locator has `uper` (a uper.Loc) and `length` None, as one with
    the locator `until:<code>` has `until` (its UntilCharsT, 0.22); a
    `size` group has `lb` and `ub` (both None with no size constraint). A
    `when` has `ranges` (0.20); a presence `named_static`, the bits named by an
    `if` outside any case that does not also hold the presence, and `chain`,
    the cases the presence stands in.
    """

    __slots__ = ("t", "lineno", "length", "value", "bindings", "name", "named", "conds", "body", "list", "mode",
                 "scope", "parts", "ends_fx", "unit", "plus", "locator", "uper", "n", "ext", "alts", "index",
                 "lb", "ub", "key", "cases", "other", "key_type", "ranges", "named_static", "chain", "key_lines",
                 "count_node", "tail_rest", "until")

    is_field = False
    is_const = False
    is_group = False

    def __init__(self, t, lineno=0, **kw):
        self.t = t
        self.lineno = lineno
        self.length = None
        self.locator = None
        self.uper = None
        self.until = None
        self.count_node = None
        self.tail_rest = False
        for k, v in kw.items():
            setattr(self, k, v)


def children(nd):
    """The line lists inside a block node (none for a line that is not a block);
    for a switch, every case and its `else` (0.20)."""
    t = nd.t
    if t == T_IF or t == T_OPT or t == T_LENGTH or t == T_EXT:
        return (nd.body,)
    if t == T_CHAIN:
        return nd.parts
    if t == T_CHOICE:
        return [a.body for a in nd.alts]
    if t == T_SWITCH:
        return [c.nodes for c in switch_cases(nd)]
    return ()


class Case:
    """One `case` or `else` body of a switch (0.20): its values (None for
    `else`), their canonical texts (set by Engine.add), its lines, its
    `default` lines, and the presence bits named by the `if` lines whose
    innermost case it is: (presence node, bit)."""

    __slots__ = ("values", "lineno", "nodes", "canon", "defaults", "if_names", "_dmap")

    def __init__(self, values, lineno):
        self.values = values
        self.lineno = lineno
        self.nodes = []
        self.canon = frozenset()
        self.defaults = []
        self.if_names = []
        self._dmap = None

    def defaults_over(self, outer):
        """{key: value} of the defaults in scope inside the case: the case's own over `outer`."""
        if not self.defaults:
            return outer
        mine = self._dmap
        if mine is None:
            mine = self._dmap = {d.key: d.value for d in self.defaults}
        out = dict(outer)
        out.update(mine)
        return out


class GroupNode(Node):
    __slots__ = ()
    is_group = True


class SeqKind:
    """A kind of the message or of the items of one group: its `when` lines
    (its first lines), its other lines, its `requires` keys and its defaults."""

    root = None

    def __init__(self, name, lineno=0):
        self.name = name
        self.lineno = lineno
        self.whens = []          # `when` nodes
        self.nodes = []
        self.requires = []
        self.requires_eq = []    # ValueCheck of each `requires <key>=<values>` term (0.20)
        self.mapped = set()      # keys mapped by a field or `also` line of the kind so far (0.20)
        self.key_lines = {}      # key -> the field nodes of the kind that map it, in reading order
        self.requires_keys = ()  # set by Engine.add
        self.requires_lists = ()
        self.defaults = []       # Default lines
        self.presences = {}
        self.lists = {}          # list -> the `if`/`opt` blocks each of its groups sits in
        self.when_lines = ()
        self.block = None        # common + whens + nodes, set when the body is closed


class Body:
    """A scope: the message, or one item of a group."""

    def __init__(self):
        self.common = []
        self.kinds = []
        self.defaults = []       # Default lines before the first kind
        self.cur = None          # the kind being read
        self.presences = {}
        self.lists = {}          # as SeqKind.lists, for the groups before the first kind
        self.first_when = None   # lineno of the first common `when`
        self.mapped = set()      # keys mapped by a common field or `also` line so far (0.20)
        self.key_lines = {}      # key -> the field nodes that map it, in reading order, while the file is read
        self._index = {}
        self._defaults = {}

    def kind(self, name):
        return self._index.get(name)

    def close(self):
        for k in self.kinds:
            k.block = self.common + k.whens + k.nodes
            k.when_lines = tuple(k.whens)

    def defaults_for(self, kind):
        """{key: value} of the defaults in scope for one message or item of `kind`."""
        key = kind.name if kind is not None else None
        out = self._defaults.get(key)
        if out is None:
            lines = self.defaults + (kind.defaults if kind is not None else [])
            out = self._defaults[key] = {d.key: d.value for d in lines}
        return out

    def bodies(self):
        """This body and every group body under it."""
        yield self

        def walk(nodes):
            for nd in nodes:
                t = nd.t
                if t == T_GROUP:
                    for b in nd.scope.bodies():
                        yield b
                else:  # a switch: every case (0.20)
                    for part in children(nd):
                        for b in walk(part):
                            yield b

        for b in walk(self.common):
            yield b
        for k in self.kinds:
            for b in walk(k.nodes):
                yield b

    def model_keys(self, add, prefix=""):
        def walk(nodes):
            for nd in nodes:
                t = nd.t
                if t == T_FIELD:
                    for f in nd.bindings:
                        add(prefix + f.key)
                elif t == T_GROUP:
                    add(prefix + nd.list + "[]")
                    nd.scope.model_keys(add, prefix + nd.list + "[].")
                else:  # a switch: every case (0.20 ruling 13)
                    for part in children(nd):
                        walk(part)

        walk(self.common)
        for k in self.kinds:
            walk(k.nodes)


def _index(lines, nd):
    for k, x in enumerate(lines):
        if x is nd:
            return k
    raise ValueError("line not in its block")


_FX_BIT = Node(T_PAD, 0, length=1)  # the `fx` bit of a chain, as a wire line


def is_wire(nd):
    """0.21 rulings 26 and 32: a line that reads or writes bits; an `if`,
    `switch` or `chain` is one only when it holds one."""
    t = nd.t
    if t == T_IF:
        return any(is_wire(x) for x in nd.body)
    if t == T_SWITCH:
        return any(is_wire(x) for c in switch_cases(nd) for x in c.nodes)
    if t == T_CHAIN:
        return len(nd.parts) > 1 or nd.ends_fx or any(is_wire(x) for part in nd.parts for x in part)
    if t == T_CHOICE:  # ruling 41: an `ext` bit, an index of more than 0 bits, or an alt with a wire line
        return nd.ext or (nd.n is not None and nd.n > 1) or any(is_wire(x) for a in nd.alts for x in a.body)
    return True  # an `ext` reads its extension bit (ruling 41)


def _plus(a, b):
    """The numbers of references along a path then another, capped at 2."""
    return {min(x + y, 2) for x in a for y in b}


def _refs(lines, count):
    """0.21 ruling 33: the possible numbers of groups that reference `count`
    along the reading paths of `lines` (capped at 2). Only a switch splits a
    path; group bodies are not entered (ruling 9)."""
    got = {0}
    for nd in lines:
        t = nd.t
        if t == T_GROUP:
            if nd.count_node is count:
                got = _plus(got, {1})
        elif t == T_SWITCH:
            sub = set()
            for c in switch_cases(nd):
                sub |= _refs(c.nodes, count)
            got = _plus(got, sub)
        else:  # `if`, `opt`, `length`, a chain, an `ext` and the `alt`s of a choice (0.19): one path
            for part in children(nd):
                got = _plus(got, _refs(part, count))
    return got


def switch_cases(nd):
    """The `case` bodies of a switch, then its `else`."""
    return nd.cases + [nd.other] if nd.other is not None else nd.cases


class _Frame:
    """One open block while the file is read."""

    __slots__ = ("what", "node", "nodes", "lineno", "presences", "body", "case")

    def __init__(self, what, node, nodes, lineno, body=None, case=None):
        self.what = what      # "group" | "if" | "opt" | "chain" | "length" | "ext" | "choice" | "switch" | "case" | "else"
        self.node = node
        self.nodes = nodes    # where the lines of the block go (a chain: its current part; a switch: None)
        self.lineno = lineno
        self.presences = {}
        self.body = body      # a group: the Body of an item
        self.case = case      # a case or else: its Case


def _uint(text):
    return int(text) if text.isascii() and text.isdigit() else None


def parse_seq_pack_text(text, *, source="pack.def"):
    """Parse one pack.def whose `layout` is `sequential`. Errors come in the
    order `parse_pack_text` states: line errors as the file is read, then the
    whole-file `bad-directive` checks (the first in line order), then
    `missing-distribution`, `missing-source`."""
    from .packdef import (
        BIT_ORDERS, HEADER_ONCE, NEEDS_WIRE, TEXT_DIRECTIVES, Default, Field, Pack, _check_transform, _Deferred,
        set_stop,
        _LineBad, armor_line, check_armor, check_encoding, line_head, parse_absent, set_bit_order,
        ValueCheck, requires_term, split_list, when_values,
    )

    seen = {}          # the header values (`line_head`)
    once = {}          # `layout`, `bit_order`
    wired = None       # the first line of NEEDS_WIRE other than `layout`, for ruling 5b of 0.11
    text_dirs = []     # `delimiter`, `start`, `checksum`: refused after the file on `bits`
    sources = []
    uses = []
    typemap = []
    tables = {}
    key_refs = []
    value_checks = []
    via_fields = []
    deferred = []      # (lineno, BadDirective)
    root = None
    msg = Body()
    stack = []         # open blocks, outermost first
    last_field = None  # the field node of the line before, for `also`: (node, transform name, options)
    armor = []         # 0.14: the `armor` lines, checked after the file (`check_armor`)
    any_kind = False   # a `kind` line read, in any body (ruling 11 of 0.14)
    wholes = []        # the `length ... whole` nodes, refused under `armor sixbit` (ruling 3 of 0.14)
    encoding = []      # 0.19: the `encoding` lines, checked after the file (`check_encoding`)
    uper_lines = []    # 0.19: (lineno, BadDirective) for each UPER line, refused when the pack is not UPER

    def line_bad(msg_text, lineno, locator=None):
        """A line check of ruling 5b of 0.11 (steps 7 to 10)."""
        return _LineBad("%s, at %s:%d" % (msg_text, source, lineno), format=seen.get("format"), locator=locator)
    rest_groups = []   # 0.21: (group node, the levels around it up to its extent, what ends the levels)
    count_lines = []   # 0.21: (count node, where it stands: ("list", lines) | ("chain", node) | ("body", body, kind))

    def bad(msg_text, lineno, locator=None):
        return BadDirective("%s, at %s:%d" % (msg_text, source, lineno), format=seen.get("format"), locator=locator)

    def body_of():
        """The scope the line stands in: the innermost open group's body, else the message."""
        for fr in reversed(stack):
            if fr.what == "group":
                return fr.body
        return msg

    def target(body):
        """Where a line goes: the innermost open block, else the kind being read, else common."""
        if stack and stack[-1].what != "group":
            return stack[-1].nodes
        return body.cur.nodes if body.cur is not None else body.common

    def in_block():
        """True when the line stands inside an `if`, `opt`, `chain` or `length` block."""
        return bool(stack) and stack[-1].what != "group"

    def find_count(name):
        """0.21: the name visible here within the current group body (ruling 9), innermost first."""
        for fr in reversed(stack):
            if fr.what == "group":
                b = fr.body
                if b.cur is not None and name in b.cur.presences:
                    return b.cur.presences[name]
                return b.presences.get(name)
            if name in fr.presences:
                return fr.presences[name]
        if msg.cur is not None and name in msg.cur.presences:
            return msg.cur.presences[name]
        return msg.presences.get(name)

    def declare(name, nd, body, lineno, what):
        """Enter a presence or count name in the table of its block (rulings 12 of 0.8, 15 of 0.21)."""
        if not name or "." in name or "!" in name or "@" in name:
            raise bad("bad %s name %r" % (what, name), lineno)
        if in_block():
            table = stack[-1].presences
            twice = name in table
        else:
            table = body.cur.presences if body.cur is not None else body.presences
            twice = name in body.presences or name in table
        if twice:  # ruling 12 of 0.8
            raise bad("the name %r is declared twice in one block" % name, lineno)
        table[name] = nd

    def home_of(body):
        """0.21: where a line being added stands, as `count_lines` records it."""
        if in_block():
            fr = stack[-1]
            if fr.what == "chain":
                return ("chain", fr.node)
            return ("list", fr.nodes)
        return ("body", body, body.cur)

    def find_presence(name):
        for fr in reversed(stack):
            if fr.what == "group":
                b = fr.body
                if b.cur is not None and name in b.cur.presences:
                    return b.cur.presences[name]
                if name in b.presences:
                    return b.presences[name]
            elif name in fr.presences:
                return fr.presences[name]
        if msg.cur is not None and name in msg.cur.presences:
            return msg.cur.presences[name]
        return msg.presences.get(name)

    def bit_len(text, what, lineno):
        """A length of 1 to 64 bits (ruling 14 of 0.8)."""
        if ":" in text:
            raise bad("a positional locator (%r) in a sequential pack; %s takes a bit length" % (text, what),
                      lineno, text)
        n = _uint(text)
        if n is None or not 1 <= n <= 64:
            raise bad("%s takes a length of 1 to 64 bits, got %r" % (what, text), lineno, text)
        return n

    def read_options(options, lineno, len_text):
        """-> {option: value}. The syntax of the options is a line check (step 9
        of ruling 5b of 0.11); `optional` in a sequential pack is not."""
        opts = {}
        i = 0
        while i < len(options):
            opt = options[i]
            if opt == "optional":
                raise bad("the field option 'optional' in a sequential pack", lineno, len_text)
            if opt not in ("via", "swap", "trim", "absent", "with", "stop"):
                raise line_bad("unknown field option %r" % opt, lineno, len_text)
            if opt in opts:
                raise line_bad("field option %r is given twice" % opt, lineno, len_text)
            if opt in ("via", "absent", "with", "stop"):  # the token after `absent` is its argument (ruling 11 of 0.9)
                if i + 1 >= len(options):
                    raise line_bad("field option %r takes %s" % (
                        opt, "a table name" if opt == "via" else "a model key" if opt == "with" else
                        "a terminator code" if opt == "stop" else "a value"),
                        lineno, len_text)
                opts[opt] = options[i + 1]
                i += 2
            else:
                opts[opt] = True
                i += 1
        return opts

    def uper_line(what, lineno, locator=None):
        """A line of a UPER pack: refused after the file is read when the pack is not one (ruling 9 of 0.19)."""
        uper_lines.append((lineno, bad("%s is a line of a UPER pack ('encoding uper'), which this pack is not"
                                       % what, lineno, locator)))

    def make_uper_field(key, len_text, tname, options, lineno, also=False):
        """A field with a UPER locator (0.19) -> (Field, uper.Loc), or (None, None).
        As the line is read: the transform's name and parameters (step 7 of
        ruling 5b of 0.11) and the syntax of the options (step 9). After the
        file is read: the locator against the table of 0.19, the transform
        against the locator ("another transform"), `swap` and `trim` (ruling
        10), `absent` (ruling 22), and whether the pack is a UPER pack (ruling 9)."""
        where = "%s:%d" % (source, lineno)
        if not also:
            _check_transform(seen.get("format"), tname, len_text, where)
            uper_line("a UPER locator (%r)" % len_text, lineno, len_text)
        opts = read_options(options, lineno, len_text)
        failed = False
        for opt in ("swap", "trim", "absent"):
            if opt in opts:
                deferred.append((lineno, bad("field option %r on a UPER locator" % opt, lineno, len_text)))
                failed = True
        try:
            loc = uper.parse_locator(len_text)
        except ValueError as e:
            deferred.append((lineno, bad(str(e), lineno, len_text)))
            return None, None
        if not uper.takes(loc.form, tname):
            deferred.append((lineno, bad("the %s locator %r does not take transform %r" % (loc.form, len_text, tname),
                                         lineno, len_text)))
            return None, None
        try:
            transform = uper.build_transform(loc.form, tname)
        except ValueError as e:
            deferred.append((lineno, bad("transform %r: %s" % (tname, e), lineno, len_text)))
            return None, None
        if failed:
            return None, None
        f = Field(key, len_text, transform, (0, 0, 0), lineno, via=opts.get("via"))
        key_refs.append((key, f))
        if "with" in opts:  # 0.18 on every wire: checked as a `requires` key is
            f.with_key = opts["with"]
            key_refs.append((f.with_key, None))
        if f.via is not None:
            via_fields.append(f)
        return f, loc

    def make_until_field(key, len_text, tname, options, lineno, also=False):
        """A field with the locator `until:<code>` (0.22) -> Field, or None.
        As the line is read: the transform's name and parameters (step 7) and
        the syntax of the options (step 9). After the file is read: the
        transform (`chars:<set>` only), the code, and the options refused with
        `until:` (`absent`, `swap`, `trim`, `stop`)."""
        where = "%s:%d" % (source, lineno)
        if not also:
            _check_transform(seen.get("format"), tname, len_text, where)
        opts = read_options(options, lineno, len_text)
        failed = False
        for opt in ("absent", "swap", "trim", "stop"):
            if opt in opts:
                deferred.append((lineno, bad("field option %r with the locator 'until:'" % opt, lineno, len_text)))
                failed = True
        if v15_family(tname) != "chars":
            deferred.append((lineno, bad("the locator 'until:' takes a 'chars:<set>' transform, got %r" % tname,
                                         lineno, len_text)))
            return None
        charset = tname.split(":", 1)[1]
        try:
            code = terminator_code(len_text[len("until:"):], charset)
        except ValueError as e:
            deferred.append((lineno, bad("the locator %r: %s" % (len_text, e), lineno, len_text)))
            return None
        if failed:
            return None
        f = Field(key, len_text, UntilCharsT(tname, charset, code), (0, 0, 0), lineno, via=opts.get("via"))
        key_refs.append((key, f))
        if "with" in opts:  # 0.18: checked as a `requires` key is
            f.with_key = opts["with"]
            key_refs.append((f.with_key, None))
        if f.via is not None:
            via_fields.append(f)
        return f

    def make_field(key, len_text, tname, options, lineno, also=False):
        """The line checks of ruling 5b of 0.11 in their order (the
        transform, the locator, the options, the values of `absent`), then the
        checks made after the file (added to `deferred`)."""
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        if not also:
            _check_transform(fmt, tname, len_text, where)
            if ":" in len_text:
                raise line_bad("a positional locator (%r) in a sequential pack; a field takes a bit length" % len_text,
                          lineno, len_text)
            length = _uint(len_text)
            if length is None or length < 1:
                raise BadLocator("bad bit length %r at %s: a sequential locator is a bit length of 1 or more"
                                 % (len_text, where), format=fmt, locator=len_text)
        else:
            length = _uint(len_text)
        opts = read_options(options, lineno, len_text)
        if not transform_valid_on(tname, "bits"):
            deferred.append((lineno, bad("transform %r is not valid on the bits wire" % tname, lineno, len_text)))
            return None
        if not bits_len_allowed(tname, length):
            deferred.append((lineno, bad(
                "transform %s does not allow a bit length of %d" % (tname, length), lineno, len_text
            )))
            if length > 64:  # the values of `absent` are not checked as the line is read (ruling 4d of 0.11)
                return None
        transform = build_bits_transform(tname, length)
        if "swap" in opts and tname in ("sixbit", "hex"):  # 0.14, 0.22
            deferred.append((lineno, bad("field option 'swap' does not apply to the '%s' transform" % tname,
                                         lineno, len_text)))
        elif "swap" in opts and v15_family(tname) in ("chars", "octal"):  # 0.15
            deferred.append((lineno, bad("field option 'swap' does not apply to the '%s' transform"
                                         % v15_family(tname), lineno, len_text)))
        elif "swap" in opts and length % 8 != 0:
            deferred.append((lineno, bad(
                "field option 'swap' needs a bit length that is a multiple of 8, got %d" % length, lineno, len_text
            )))
        if "trim" in opts:
            if tname not in ("ascii", "sixbit"):  # ruling 5 of 0.14
                deferred.append((lineno, bad("field option 'trim' applies to the 'ascii' and 'sixbit' "
                                             "transforms only", lineno, len_text)))
            else:
                transform.trim = True
        if "stop" in opts:  # 0.22
            why = set_stop(transform, tname, opts["stop"])
            if why is not None:
                deferred.append((lineno, bad(why, lineno, len_text)))
        f = Field(key, len_text, transform, (0, 0, length), lineno, via=opts.get("via"), swap="swap" in opts)
        if "absent" in opts:
            try:
                f.absent, f.absent_out = parse_absent("bits", transform, length, opts["absent"], fmt, len_text, where)
            except _Deferred as d:
                deferred.append((lineno, d.err))
                return None
        key_refs.append((key, f))
        if "with" in opts:  # 0.18: checked as a `requires` key is
            f.with_key = opts["with"]
            key_refs.append((f.with_key, None))
        if f.via is not None:
            via_fields.append(f)
        return f

    def cases_open():
        """The cases the line stands in, outermost first (0.20)."""
        return tuple(fr.case for fr in stack if fr.case is not None)

    def mapped(body, f, nd):
        """A field or `also` line maps its key in its scope: for a later `switch` (0.20)."""
        owner = body.cur if body.cur is not None else body
        owner.mapped.add(f.key)
        lines = owner.key_lines.setdefault(f.key, [])
        if nd not in lines:
            lines.append(nd)

    def value_node(t, kw, args, lineno):
        text = args[0]
        if text.startswith(uper.PREFIXES):
            # rulings 9 and 10 of 0.19: `when` and `const` take bit lengths only; a
            # UPER locator is not a locator form of step 8, so it is refused after the file
            raise bad("a '%s' takes a bit length, not the UPER locator %r" % (kw, text), lineno, text)
        if ":" not in text and (_uint(text) is None or _uint(text) == 0):  # ruling 5a of 0.11
            raise BadLocator("bad bit length %r at %s:%d: a sequential locator is a bit length of 1 or more"
                             % (text, source, lineno), format=seen.get("format"), locator=text)
        if ":" in text:
            raise line_bad("a positional locator (%r) in a sequential pack" % text, lineno, text)
        length = bit_len(text, "a '%s'" % kw, lineno)
        if t == T_WHEN:  # 0.20: a list of values and ranges, checked after the file
            try:
                w = when_values("bits", args[1], length, lambda m: bad(m, lineno, text),
                                lambda m: _Deferred(bad(m, lineno, text)), lambda **kw: kw)
            except _Deferred as d:
                deferred.append((lineno, d.err))
                w = {"value": 0, "ranges": ((0, 0),)}
            except BadDirective as e:
                deferred.append((lineno, e))
                w = {"value": 0, "ranges": ((0, 0),)}
            return Node(t, lineno, length=length, value=w["value"], ranges=w["ranges"], locator=text)
        try:
            n = read_uint_text(args[1])  # ruling 2c of 0.11
        except ValueError:
            deferred.append((lineno, bad("a bits '%s' value is an unsigned decimal integer, got %r"
                                         % (kw, args[1]), lineno, args[0])))
            n = 0
        if n >= (1 << length):
            deferred.append((lineno, bad("'%s' value %d does not fit %d bit(s)" % (kw, n, length), lineno, args[0])))
        return Node(t, lineno, length=length, value=n, locator=args[0])

    def rest_levels(nd, body):
        """0.21: the blocks around a `rest` group, innermost first, up to its
        extent: [(level, child)] where level is ("list", lines), ("chain",
        node) or ("body", body, kind), and child the node of that level that
        holds the group; then what ends them: "extent" (a `length` block or the
        message) or the group node whose item body holds it."""
        levels = []
        child = nd
        for fr in reversed(stack):
            w = fr.what
            if w == "switch":
                continue  # its case stood for it
            if w == "group":
                levels.append((("body", fr.body, fr.body.cur), child))
                return levels, fr.node
            if w == "chain":
                levels.append((("chain", fr.node), child))
            elif w == "case" or w == "else":
                levels.append((("list", fr.case.nodes), child))
            else:
                levels.append((("list", fr.nodes), child))
            if w == "length":
                return levels, "extent"
            child = fr.node
        levels.append((("body", msg, msg.cur), child))
        return levels, "extent"

    def after(level, child):
        """The lines read after `child` in one level, to the end of that level."""
        if level[0] == "list":
            lines = level[1]
            return lines[_index(lines, child) + 1:]
        if level[0] == "chain":
            parts = level[1].parts
            for k, part in enumerate(parts):
                if any(x is child for x in part):
                    out = part[_index(part, child) + 1:]
                    if k < len(parts) - 1 or level[1].ends_fx:
                        out = out + [_FX_BIT]  # an `fx` bit follows
                    for later in parts[k + 1:]:
                        out = out + later
                    return out
            return []
        _, b, kind = level
        if kind is not None:
            return kind.nodes[_index(kind.nodes, child) + 1:]
        out = b.common[_index(b.common, child) + 1:]
        for k in b.kinds:
            out = out + k.whens + k.nodes
        return out

    def check_rest(nd, found, lineno):
        """Rulings 10, 11, 26, 27 and 32 of 0.21: a `rest` group is the last wire
        line of its extent; inside an item it stands within a `length` block of
        the item's body or as the last line of an outer `rest` item."""
        levels, end = found
        for level, child in levels:
            for ln in after(level, child):
                if is_wire(ln):
                    deferred.append((lineno, bad("a 'rest' group is not the last line of its extent (line %d is "
                                                 "read after it)" % ln.lineno, lineno, "rest")))
                    return
        if end != "extent":
            if end.mode != "rest":
                deferred.append((lineno, bad("a 'rest' group inside an item stands within a 'length' block of the "
                                             "item's body, or last in an item of a 'rest' group", lineno, "rest")))
                return
            nd.tail_rest = True

    def check_count(nd, home):
        """Rulings 2 and 33 of 0.21: in each reading path of the count's scope
        exactly one group references it."""
        if home[0] == "body" and home[2] is None and home[1].kinds:
            b = home[1]
            first = _refs(b.common[_index(b.common, nd) + 1:], nd)
            paths = [_plus(first, _refs(k.nodes, nd)) for k in b.kinds]
        else:
            paths = [_refs(after(home, nd), nd)]
        for got in paths:
            if got != {1}:
                deferred.append((nd.lineno, bad(
                    "count %r is referenced by %s group in a reading path; exactly one 'count @%s' is needed"
                    % (nd.name, "no" if 0 in got else "more than one", nd.name), nd.lineno, nd.locator)))
                return

    def close_body(body, lineno):
        if body.first_when is not None and not body.kinds:
            deferred.append((body.first_when, bad("'when' in a body with no 'kind' line", body.first_when)))
        body.close()

    for lineno, raw in enumerate(text.splitlines(), 1):
        head = line_head(raw, source, lineno, seen)
        if head is None:
            continue
        kw, args = head
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        prev_field, last_field = last_field, None
        try:
            if wired is None and kw in NEEDS_WIRE and kw != "layout":
                wired = kw
            if stack and stack[-1].what == "switch" and kw not in ("case", "else", "end"):  # ruling 20 of 0.20
                raise bad("%r between a 'switch' and its first 'case'" % kw, lineno)
            if stack and kw in _HEADER:
                raise bad("%r inside a block" % kw, lineno)
            if stack and stack[-1].what == "choice" and not stack[-1].node.alts and kw not in ("alt", "end"):
                deferred.append((lineno, bad("a line between 'choice' and its first 'alt'", lineno)))
            body = body_of()

            if kw in HEADER_ONCE:
                pass  # read by `line_head`
            elif kw in _SINGLE:
                if kw in once:
                    raise bad("a second %r line" % kw, lineno)
                once[kw] = args[0]
                if kw == "bit_order" and args[0] not in BIT_ORDERS:
                    raise bad("bit_order must be one of %s, got %r" % (", ".join(BIT_ORDERS), args[0]), lineno,
                              "bit_order")
                if kw == "layout":
                    if args[0] != "sequential":
                        raise bad("a second layout, %r, in a sequential pack" % args[0], lineno, "layout")
                    if wired is not None:  # ruling 5b of 0.11
                        raise bad("'layout sequential' after a %r line" % wired, lineno, "layout")
            elif kw in TEXT_DIRECTIVES or kw == "tag" or kw == "key" or kw == "element":
                # `delimiter`, `start`, `checksum` (0.10); `tag`, `key`, `checksum sum16` (0.13);
                # `element` (0.18, xml): another wire's
                text_dirs.append((lineno, bad("%r is a directive of another wire" % kw, lineno, kw)))
            elif kw == "armor":
                armor_line(args, armor, any_kind, lineno)
            elif kw == "encoding":  # 0.19: checked after the file is read (`check_encoding`)
                encoding.append((args[0], lineno, any_kind))
            elif kw == "ext":
                uper_line("'ext'", lineno)
                nd = Node(T_EXT, lineno, body=[])
                target(body).append(nd)
                stack.append(_Frame("ext", nd, nd.body, lineno))
            elif kw == "choice":
                uper_line("'choice'", lineno)
                n = uper.parse_number(args[0])
                if n is None or not 1 <= n <= 65536:
                    deferred.append((lineno, bad("'choice' takes 1 to 65536 alternatives, got %r" % args[0], lineno)))
                    n = None
                if len(args) == 2 and args[1] != "ext":
                    deferred.append((lineno, bad("a 'choice' line is: choice <n> [ext]", lineno)))
                nd = Node(T_CHOICE, lineno, n=n, ext=len(args) == 2, alts=[], index={})
                target(body).append(nd)
                stack.append(_Frame("choice", nd, [], lineno))
            elif kw == "alt":
                if not stack or stack[-1].what != "choice":
                    raise bad("'alt' outside a 'choice'", lineno)
                fr = stack[-1]
                ch = fr.node
                i = uper.parse_number(args[0])
                if i is None or (ch.n is not None and i >= ch.n):
                    deferred.append((lineno, bad("'alt' index %r is outside 0..n-1 of its choice" % args[0], lineno)))
                elif i in ch.index:
                    deferred.append((lineno, bad("'alt' index %d is given twice" % i, lineno)))
                alt = Node(T_ALT, lineno, value=i, body=[])
                ch.alts.append(alt)
                if i is not None and i not in ch.index:
                    ch.index[i] = alt
                fr.nodes = alt.body
                fr.presences = {}  # each `alt` is a block of its own for presence names (ruling 21 of 0.19)
            elif kw == "word_bytes":
                deferred.append((lineno, bad("'word_bytes' is not used in a sequential pack", lineno, "word_bytes")))
            elif kw == "root":
                if root is not None:
                    raise bad("a second 'root' line", lineno)
                root = args[0]
                deferred.append((lineno, bad("'root' is xml only", lineno, "root")))
            elif kw == "source":
                sources.append(args[0])
            elif kw == "uses":
                if args[0] not in uses:
                    uses.append(args[0])
            elif kw == "map":
                tables.setdefault(args[0], []).append((args[1], args[2]))
            elif kw == "typemap":
                typemap.append((lineno, args))
            elif kw == "kind":
                if in_block():
                    raise bad("'kind' inside a block", lineno)
                if body.kind(args[0]) is not None:
                    raise DuplicateKind("kind %r is declared twice in one body, at %s" % (args[0], where), format=fmt)
                body.cur = SeqKind(args[0], lineno)
                any_kind = True
                body.kinds.append(body.cur)
                body._index[args[0]] = body.cur
            elif kw == "requires":
                if in_block():
                    raise bad("'requires' inside a block", lineno)
                if body.cur is None:
                    raise bad("'requires' before the first 'kind'", lineno)
                for term in args:
                    key, values = requires_term(term, lambda m: bad(m, lineno))
                    if key not in body.cur.requires:
                        body.cur.requires.append(key)
                    if values is not None:
                        vc = ValueCheck("requires", key, values, lineno, body.cur)
                        body.cur.requires_eq.append(vc)
                        value_checks.append(vc)
                    key_refs.append((key, None))
            elif kw == "default":
                d = Default(args[0], args[1], lineno)
                case = None
                for fr in reversed(stack):  # 0.20 ruling 7: a default in a case is the case's
                    if fr.what == "group":
                        break
                    if fr.case is not None:
                        case = fr.case
                        break
                if case is not None:
                    case.defaults.append(d)
                else:
                    (body.cur.defaults if body.cur is not None else body.defaults).append(d)
                key_refs.append((args[0], d))
            elif kw == "when":
                if in_block():  # ruling 10 of 0.8
                    raise bad("'when' inside a block", lineno, args[0])
                nd = value_node(T_WHEN, kw, args, lineno)
                if body.cur is None:  # a common `when` of 0.5
                    body.common.append(nd)
                    if body.first_when is None:
                        body.first_when = lineno
                elif body.cur.nodes:
                    raise bad("'when' after another line of its kind", lineno, args[0])
                else:
                    body.cur.whens.append(nd)
            elif kw == "field":
                until = None
                if args[1].startswith(uper.PREFIXES):  # 0.19
                    f, loc = make_uper_field(args[0], args[1], args[2], args[3:], lineno)
                elif args[1].startswith("until:"):  # 0.22
                    f, loc = make_until_field(args[0], args[1], args[2], args[3:], lineno), None
                    if f is not None:
                        until = f.transform
                else:
                    f, loc = make_field(args[0], args[1], args[2], args[3:], lineno), None
                if f is not None:
                    nd = Node(T_FIELD, lineno, length=None if loc is not None or until is not None else f.loc[2],
                              bindings=[f], locator=args[1], uper=loc, until=until)
                    target(body).append(nd)
                    last_field = (nd, args[2], args[3:])
                    mapped(body, f, nd)
            elif kw == "also":
                if prev_field is None:  # ruling 13 of 0.8
                    raise bad("'also' is written directly after a 'field' line", lineno)
                nd, tname, options = prev_field
                # `swap`, `trim` and `absent` describe the bits, so the `also` key shares them.
                carried = []
                k = 0
                while k < len(options):
                    if options[k] in ("via", "absent", "with"):  # `with` is the line's own (ruling 5 of 0.18)
                        if options[k] == "absent":
                            carried.extend(options[k:k + 2])
                        k += 2
                        continue
                    if options[k] in ("swap", "trim"):
                        carried.append(options[k])
                    k += 1
                extra = args[1:]
                if extra and (len(extra) != 2 or extra[0] != "via"):
                    raise bad("an 'also' line is: also <key> [via <table>]", lineno)
                if nd.uper is not None:  # the `also` key shares the UPER locator and its transform
                    f = make_uper_field(args[0], nd.locator, tname, carried + extra, lineno, also=True)[0]
                elif nd.until is not None:  # 0.22: the `also` key shares the terminated characters
                    f = make_until_field(args[0], nd.locator, tname, carried + extra, lineno, also=True)
                else:
                    f = make_field(args[0], nd.locator, tname, carried + extra, lineno, also=True)
                if f is not None:
                    nd.bindings.append(f)
                    mapped(body, f, nd)
                last_field = prev_field
            elif kw == "const":
                target(body).append(value_node(T_CONST, kw, args, lineno))
            elif kw == "pad" or kw == "align":
                if ":" in args[0]:
                    raise bad("a positional locator (%r) in a sequential pack" % args[0], lineno, args[0])
                n = _uint(args[0])
                if n is None or n < 1:
                    raise bad("'%s' takes a number of bits of 1 or more, got %r" % (kw, args[0]), lineno, args[0])
                target(body).append(Node(T_PAD if kw == "pad" else T_ALIGN, lineno, length=n, locator=args[0]))
            elif kw == "presence":
                name = args[0]
                if stack and stack[-1].what == "ext" and stack[-1].nodes:  # ruling 15 of 0.19
                    raise bad("a 'presence' line inside an 'ext' block is its first line", lineno)
                length = None if args[1] == "fx" else bit_len(args[1], "a 'presence'", lineno)
                nd = Node(T_PRESENCE, lineno, name=name, length=length, named=set(), locator=args[1],
                          named_static=set(), chain=cases_open())
                declare(name, nd, body, lineno, "presence")  # one namespace with counts (ruling 15 of 0.21)
                target(body).append(nd)
            elif kw == "count":  # 0.21: `count <name> <len> [plus <n>]`
                name = args[0]
                length = bit_len(args[1], "a 'count'", lineno)
                plus = 0
                opts = args[2:]
                if opts:
                    # ruling 16: an optional `-` then digits, within signed 64 bits
                    if len(opts) != 2 or opts[0] != "plus":
                        raise bad("a 'count' line takes only 'plus <n>', got %r" % " ".join(opts), lineno, args[1])
                    ptext = opts[1]
                    digits = ptext[1:] if ptext.startswith("-") else ptext
                    if not (digits.isascii() and digits.isdigit()):
                        raise bad("'plus' takes a decimal integer, got %r" % ptext, lineno, args[1])
                    plus = int(ptext)
                    if not -(1 << 63) <= plus < (1 << 63):
                        raise bad("'plus' %s is outside signed 64 bits" % ptext, lineno, args[1])
                nd = Node(T_COUNT, lineno, name=name, length=length, plus=plus, locator=args[1])
                declare(name, nd, body, lineno, "count")
                count_lines.append((nd, home_of(body)))
                target(body).append(nd)
            elif kw == "if":
                # 0.8's `if` (presence names); a 0.17 gate locator here is
                # `bad-directive` after the file. A failing `if` still opens its
                # block, so that its `end` closes it and not an enclosing block.
                conds = []
                failure = None
                for term in args:
                    neg = term.startswith("!")
                    name, dot, bit = (term[1:] if neg else term).rpartition(".")
                    n = _uint(bit)
                    if not dot or not name or n is None or n < 1:
                        failure = bad("an 'if' term is [!]<name>.<i> with i from 1, got %r" % term, lineno)
                        break
                    p = find_presence(name)
                    if p is None:
                        failure = bad("'if' names presence %r, which is not visible here" % name, lineno)
                        break
                    if p.t == T_COUNT:  # ruling 15 of 0.21
                        failure = bad("'if' names %r, which is a count" % name, lineno)
                        break
                    if p.length is not None and n > p.length:
                        failure = bad("'if' names bit %d of presence %r, which has %d bit(s)"
                                      % (n, name, p.length), lineno)
                        break
                    p.named.add(n)
                    # 0.20 ruling 21: the bit counts only when the innermost case the
                    # `if` sits in, and the presence does not, is the one read
                    case = None
                    for c in reversed(cases_open()):
                        if c not in p.chain:
                            case = c
                            break
                    if case is None:
                        p.named_static.add(n)
                    else:
                        case.if_names.append((p, n))
                    conds.append((p, n, neg))
                nd = Node(T_IF, lineno, conds=conds, body=[])
                target(body).append(nd)
                stack.append(_Frame("if", nd, nd.body, lineno))
                if failure is not None:
                    raise failure
            elif kw == "opt":
                nd = Node(T_OPT, lineno, body=[])
                target(body).append(nd)
                stack.append(_Frame("opt", nd, nd.body, lineno))
            elif kw == "chain":
                nd = Node(T_CHAIN, lineno, parts=[[]], ends_fx=False)
                target(body).append(nd)
                stack.append(_Frame("chain", nd, nd.parts[0], lineno))
            elif kw == "fx":
                if not stack or stack[-1].what != "chain":
                    raise bad("'fx' is a line of a 'chain' block", lineno)
                fr = stack[-1]
                fr.node.parts.append([])
                fr.nodes = fr.node.parts[-1]
            elif kw == "length":
                length = bit_len(args[0], "a 'length'", lineno)
                unit = None
                plus = None
                whole = False
                i = 1
                while i < len(args):
                    opt = args[i]
                    if opt in ("bytes", "bits") and unit is None:
                        unit = opt
                    elif opt == "whole" and not whole:
                        whole = True
                    elif opt == "plus" and plus is None and i + 1 < len(args):
                        i += 1
                        try:
                            plus = read_int_text(args[i])  # integer text (ruling 2c of 0.11)
                        except ValueError:
                            raise bad("'plus' takes an integer, got %r" % args[i], lineno, args[0])
                    else:
                        raise bad("a 'length' line is: length <len> [bytes|bits] [plus <n>] [whole]; got %r" % opt,
                                  lineno, args[0])
                    i += 1
                if whole:
                    if unit == "bits":  # ruling 8 of 0.8
                        raise bad("'length ... bits whole': the whole message is counted in bytes", lineno, args[0])
                    nd = Node(T_WHOLE, lineno, length=length, plus=plus or 0, locator=args[0])
                    target(body).append(nd)
                    wholes.append(nd)
                else:
                    nd = Node(T_LENGTH, lineno, length=length, unit=unit or "bytes", plus=plus or 0, body=[],
                              locator=args[0])
                    target(body).append(nd)
                    stack.append(_Frame("length", nd, nd.body, lineno))
            elif kw == "group":
                name, mode = args[0], args[1]
                if ":" in mode:
                    raise line_bad("a positional group locator (%r) in a sequential pack" % mode, lineno, mode)
                lb = ub = None
                if mode == "size":  # 0.19
                    if len(args) not in (2, 4):
                        raise bad("a 'group ... size' line is: group <list> size [<lb> <ub>]", lineno, mode)
                    uper_line("'group ... size'", lineno, mode)
                    if len(args) == 4:
                        lb, ub = uper.parse_number(args[2]), uper.parse_number(args[3])
                        if lb is None or ub is None or not 0 <= lb <= ub < 65536:  # ruling 12 of 0.19
                            # after the file is read; the block is still opened, so that its `end` closes it
                            deferred.append((lineno, bad("'size' takes 0 <= lb <= ub < 65536, got %s %s"
                                                         % (args[2], args[3]), lineno, mode)))
                            lb, ub = 0, 0
                elif mode not in GROUP_MODES or len(args) != (3 if mode == "count" else 2):
                    raise bad("a sequential 'group' line is: group <list> count <len> | count @<name> | while | "
                              "more | fx | rest | last", lineno, mode)
                length = None
                count_node = None
                if mode == "count" and args[2].startswith("@"):  # 0.21: the count of a `count` line
                    cname = args[2][1:]
                    count_node = find_count(cname) if cname else None
                    if count_node is None:
                        raise bad("'count @%s' names no count visible in this group body" % cname, lineno, mode)
                    if count_node.t != T_COUNT:  # ruling 15
                        raise bad("'count @%s' names a presence" % cname, lineno, mode)
                elif mode == "count":
                    length = bit_len(args[2], "a group 'count'", lineno)
                # Ruling 23 of 0.8: a list in two groups of one scope is allowed
                # when the two groups sit in different `if` or `opt` blocks. A
                # group in no such block, two groups in one block, and a group in
                # a block that encloses the other's are refused.
                blocks = []
                for fr in reversed(stack):
                    if fr.what == "group":
                        break
                    if fr.what == "if" or fr.what == "opt":
                        blocks.append(fr.node)
                    elif fr.what == "choice" and fr.node.alts:  # ruling 20 of 0.19: different alts
                        blocks.append(fr.node.alts[-1])
                    elif fr.case is not None:  # ruling 10 of 0.20: each case is a block
                        blocks.append(fr.case)
                lists = body.cur.lists if body.cur is not None else body.lists
                for table in ((body.lists, lists) if lists is not body.lists else (lists,)):
                    for other in table.get(name, ()):
                        if not blocks or not other or blocks[0] in other or other[0] in blocks:
                            raise bad("list %s is mapped by two groups of one scope that are not in different "
                                      "'if' or 'opt' blocks" % name, lineno, mode)
                lists.setdefault(name, []).append(blocks)
                nd = GroupNode(T_GROUP, lineno, list=name, mode=mode, length=length, scope=Body(), locator=mode,
                               lb=lb, ub=ub)
                nd.count_node = count_node
                if mode == "rest":
                    rest_groups.append((nd, rest_levels(nd, body), lineno))
                target(body).append(nd)
                key_refs.append((name, nd))
                stack.append(_Frame("group", nd, None, lineno, body=nd.scope))
            elif kw == "switch":  # 0.20
                key = args[0]
                if key not in body.mapped and (body.cur is None or key not in body.cur.mapped):
                    raise bad("'switch %s': the key is not mapped by a field or 'also' line before the switch in "
                              "its scope" % key, lineno, key)
                nd = Node(T_SWITCH, lineno, key=key, cases=[], other=None, key_type=None, locator=key,
                          key_lines=tuple(body.key_lines.get(key, ())) + (
                              tuple(body.cur.key_lines.get(key, ())) if body.cur is not None else ()))
                target(body).append(nd)
                stack.append(_Frame("switch", nd, None, lineno))
            elif kw == "case" or kw == "else":  # 0.20
                if stack and stack[-1].what in ("case", "else"):
                    stack.pop()
                if not stack or stack[-1].what != "switch":
                    raise bad("%r outside a 'switch'" % kw, lineno)
                sw = stack[-1].node
                if kw == "case":
                    if sw.other is not None:
                        raise bad("'case' after the 'else' of its switch", lineno)
                    values = split_list(args[0], lambda m: bad(m, lineno))
                    c = Case(values, lineno)
                    sw.cases.append(c)
                    value_checks.append(ValueCheck("case", sw.key, values, lineno, c, switch=sw))
                else:
                    if sw.other is not None:
                        raise bad("a second 'else' in one switch", lineno)
                    if not sw.cases:
                        raise bad("'else' before the first 'case' of its switch", lineno)
                    c = Case(None, lineno)
                    sw.other = c
                stack.append(_Frame(kw, sw, c.nodes, lineno, case=c))
            else:  # end
                if not stack:
                    raise bad("'end' with no open block", lineno)
                fr = stack.pop()
                if fr.what == "switch":
                    raise bad("a 'switch' with no 'case'", lineno)
                if fr.what == "case" or fr.what == "else":
                    stack.pop()  # the switch
                elif fr.what == "group":
                    close_body(fr.body, lineno)
                elif fr.what == "chain":
                    parts = fr.node.parts
                    if len(parts) > 1 and not parts[-1]:  # the chain ends with an `fx` line
                        parts.pop()
                        fr.node.ends_fx = True
                elif fr.what == "choice" and not fr.node.alts:  # ruling 19 of 0.19
                    deferred.append((fr.lineno, bad("a 'choice' with no 'alt' line", fr.lineno)))
        except _LineBad:
            raise
        except (BadDirective, DuplicateKind) as e:  # not a line check: after the file (ruling 5b of 0.11)
            deferred.append((lineno, e))

    name = seen.get("format")
    if not stack:  # 0.21: checks that need every line of the block
        for nd, levels, lineno in rest_groups:
            check_rest(nd, levels, lineno)
        for nd, home in count_lines:
            check_count(nd, home)
    version = seen.get("version")
    if not name:
        raise MissingFormat("the pack has no 'format' line in %s" % source)
    if not version:
        raise MissingVersion("the pack has no 'version' line in %s" % source, format=name)
    deferred.extend(text_dirs)
    for fr in stack:
        if fr.what == "case" or fr.what == "else":
            continue  # reported as its switch
        deferred.append((fr.lineno, bad("%r with no 'end'" % fr.what, fr.lineno)))
        if fr.what == "group":
            fr.body.close()
    close_body(msg, 0)
    armor_value = check_armor(armor, "bits", once.get("bit_order"), deferred, source, name)
    is_uper = check_encoding(encoding, "bits", True, once.get("bit_order"), armor_value, deferred, source,
                             name) == "uper"
    if not is_uper:  # ruling 9 of 0.19
        deferred.extend(uper_lines)
    if armor_value == "sixbit":
        for nd in wholes:
            deferred.append((nd.lineno, bad("'length ... whole' under 'armor sixbit': the armored text has no "
                                            "whole-byte length", nd.lineno, nd.locator)))
    for f in via_fields:
        if f.via not in tables:
            deferred.append((f.lineno, bad("'via' names table %r, which has no 'map' line" % f.via, f.lineno,
                                           f.locator)))
    if deferred:
        raise min(deferred, key=lambda d: d[0])[1]
    distribution = seen.get("distribution")
    if distribution is None:
        raise MissingDistribution("pack has no 'distribution' line in %s" % source, format=name)
    if not sources:
        raise MissingSource("pack has no 'source' line in %s" % source, format=name)

    pack = Pack(
        name=name, version=version, wire="bits", bit_order=once.get("bit_order", "lsb"), kinds=msg.kinds,
        key_refs=key_refs, distribution=distribution, sources=sources, uses=uses, tables=tables,
        armor=armor_value, value_checks=value_checks,
    )
    pack.layout = "sequential"
    pack.seq = msg
    pack.uper = is_uper
    set_bit_order(pack)
    for lineno, (native, cot) in typemap:
        if native in pack.native_to_cot or cot in pack.cot_to_native:
            raise BadDirective(
                "typemap %r <-> %r is not one-to-one at %s:%d" % (native, cot, source, lineno), format=name
            )
        pack.native_to_cot[native] = cot
        pack.cot_to_native[cot] = native
    return pack
