"""pack.def parsing for `layout sequential` (spec/engine-spec.md, "Version
0.8: sequential binary layouts"): a `bits` pack whose lines are read in order
from a cursor. A locator is a bit length, not a position.

The file becomes a tree of `Node`s. A `Body` is a scope: the message, or the
body of one group. It holds the lines before the first `kind` (common) and the
kinds; one item, like one message, is the common lines plus one kind.
"""

from .errors import (
    BadDirective, DuplicateKind, InvalidPack, MissingDistribution, MissingSource, UnknownDirective,
    UnknownTransform,
)
from .transforms import (
    TEXT_TRANSFORM_NAMES, bits_len_allowed, build_bits_transform, is_protobuf_transform_name,
)

(T_FIELD, T_CONST, T_PAD, T_ALIGN, T_WHEN, T_PRESENCE, T_IF, T_OPT, T_GROUP, T_CHAIN, T_LENGTH,
 T_WHOLE) = range(12)

GROUP_MODES = ("count", "while", "more", "fx")
_HEADER = ("format", "version", "wire", "layout", "bit_order", "word_bytes", "distribution", "source", "uses",
           "map", "typemap", "root")
_SINGLE = ("format", "version", "wire", "layout", "bit_order", "distribution")
_ARITY = {
    "format": 1, "version": 1, "wire": 1, "layout": 1, "bit_order": 1, "word_bytes": 1, "distribution": 1,
    "source": 1, "uses": 1, "map": 3, "typemap": 2, "root": 1, "kind": 1, "when": 2, "const": 2, "default": 2,
    "pad": 1, "align": 1, "presence": 2, "opt": 0, "chain": 0, "fx": 0, "end": 0,
    # `field` three or more, `also` one or three, `requires` and `if` one or more, `group` two or
    # three, `length` one or more
    "field": None, "also": None, "requires": None, "if": None, "group": None, "length": None,
}


class Node:
    """One line of a sequential pack, with the lines of its block.

    field     `bindings`: the `Field` of the line and of each `also` line after it; `length`
    const     `length`, `value`              when   `length`, `value`
    pad       `length`                       align  `length` (the multiple)
    presence  `name`, `length` (None with `fx`), `named`: the bits some `if` names
    if        `conds`: (presence node, bit, negated); `body`
    opt       `body`
    group     `list`, `mode`, `length` (the count's bits), `scope`: the Body of an item
    chain     `parts`: the lines between `fx` lines; `ends_fx`: the last line is `fx`
    length    `length`, `unit` ("bytes" | "bits"), `plus`, `body`
    whole     `length`, `plus`
    """

    __slots__ = ("t", "lineno", "length", "value", "bindings", "name", "named", "conds", "body", "list", "mode",
                 "scope", "parts", "ends_fx", "unit", "plus", "locator")

    is_field = False
    is_const = False
    is_group = False

    def __init__(self, t, lineno=0, **kw):
        self.t = t
        self.lineno = lineno
        self.length = None
        self.locator = None
        for k, v in kw.items():
            setattr(self, k, v)


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
                elif t == T_IF or t == T_OPT or t == T_LENGTH:
                    for b in walk(nd.body):
                        yield b
                elif t == T_CHAIN:
                    for part in nd.parts:
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
                elif t == T_IF or t == T_OPT or t == T_LENGTH:
                    walk(nd.body)
                elif t == T_CHAIN:
                    for part in nd.parts:
                        walk(part)

        walk(self.common)
        for k in self.kinds:
            walk(k.nodes)


class _Frame:
    """One open block while the file is read."""

    __slots__ = ("what", "node", "nodes", "lineno", "presences", "body")

    def __init__(self, what, node, nodes, lineno, body=None):
        self.what = what      # "group" | "if" | "opt" | "chain" | "length"
        self.node = node
        self.nodes = nodes    # where the lines of the block go (a chain: its current part)
        self.lineno = lineno
        self.presences = {}
        self.body = body      # a group: the Body of an item


def _uint(text):
    return int(text) if text.isascii() and text.isdigit() else None


def parse_seq_pack_text(text, *, source="pack.def"):
    """Parse one pack.def whose `layout` is `sequential`. Errors come in the
    order `parse_pack_text` states: line errors as the file is read, then the
    whole-file `bad-directive` checks (the first in line order), then
    `missing-distribution`, `missing-source`."""
    from .packdef import Default, Field, Pack, WIRES, BIT_ORDERS, DISTRIBUTIONS, tokenize

    seen = {}
    sources = []
    uses = []
    typemap = []
    tables = {}
    key_refs = []
    via_fields = []
    deferred = []      # (lineno, BadDirective)
    root = None
    msg = Body()
    stack = []         # open blocks, outermost first
    last_field = None  # the field node of the line before, for `also`: (node, transform name, options)

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

    def make_field(key, len_text, tname, options, lineno):
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        if ":" in len_text:
            raise bad("a positional locator (%r) in a sequential pack; a field takes a bit length" % len_text,
                      lineno, len_text)
        length = _uint(len_text)
        if length is None or length < 1:
            raise InvalidPack("bad bit length %r at %s" % (len_text, where), format=fmt, locator=len_text)
        opts = {}
        i = 0
        while i < len(options):
            opt = options[i]
            if opt == "optional":
                raise bad("the field option 'optional' in a sequential pack", lineno, len_text)
            if opt not in ("via", "swap", "trim"):
                raise bad("unknown field option %r" % opt, lineno, len_text)
            if opt in opts:
                raise bad("field option %r is given twice" % opt, lineno, len_text)
            if opt == "via":
                if i + 1 >= len(options):
                    raise bad("field option 'via' takes a table name", lineno, len_text)
                opts[opt] = options[i + 1]
                i += 2
            else:
                opts[opt] = True
                i += 1
        if tname in TEXT_TRANSFORM_NAMES or is_protobuf_transform_name(tname):
            raise InvalidPack("transform %r is not valid on a bits wire" % tname, format=fmt, locator=len_text)
        if tname in ("f32", "f64", "ascii"):
            if not bits_len_allowed(tname, length):
                deferred.append((lineno, bad(
                    "transform %s does not allow a bit length of %d" % (tname, length), lineno, len_text
                )))
                return None
        elif length > 64:
            raise InvalidPack("bad bit length at %s: len must be in 1..=64" % where, format=fmt, locator=len_text)
        try:
            transform = build_bits_transform(tname, length)
        except ValueError as e:
            raise UnknownTransform("unknown transform %r at %s: %s" % (tname, where, e), format=fmt, locator=len_text)
        if "swap" in opts and length % 8 != 0:
            deferred.append((lineno, bad(
                "field option 'swap' needs a bit length that is a multiple of 8, got %d" % length, lineno, len_text
            )))
        if "trim" in opts:
            if tname != "ascii":
                deferred.append((lineno, bad("field option 'trim' applies to the 'ascii' transform only",
                                             lineno, len_text)))
            else:
                transform.trim = True
        f = Field(key, len_text, transform, (0, 0, length), lineno, via=opts.get("via"), swap="swap" in opts)
        key_refs.append((key, f))
        if f.via is not None:
            via_fields.append(f)
        return f

    def value_node(t, kw, args, lineno):
        length = bit_len(args[0], "a '%s'" % kw, lineno)
        n = _uint(args[1])
        if n is None:
            raise bad("a bits '%s' value is an unsigned decimal integer, got %r" % (kw, args[1]), lineno, args[0])
        if n >= (1 << length):
            deferred.append((lineno, bad("'%s' value %d does not fit %d bit(s)" % (kw, n, length), lineno, args[0])))
        return Node(t, lineno, length=length, value=n, locator=args[0])

    def close_body(body, lineno):
        if body.first_when is not None and not body.kinds:
            deferred.append((body.first_when, bad("'when' in a body with no 'kind' line", body.first_when)))
        body.close()

    for lineno, raw in enumerate(text.splitlines(), 1):
        toks = tokenize(raw, source=source, lineno=lineno)
        if not toks:
            continue
        kw, args = toks[0], toks[1:]
        fmt = seen.get("format")
        where = "%s:%d" % (source, lineno)
        if kw not in _ARITY:
            raise UnknownDirective("unknown directive %r at %s" % (kw, where), format=fmt)
        prev_field, last_field = last_field, None
        if stack and kw in _HEADER:
            raise bad("%r inside a block" % kw, lineno)
        want = _ARITY[kw]
        if want is None:
            low = {"field": 3, "also": 1, "requires": 1, "if": 1, "group": 2, "length": 1}[kw]
            if len(args) < low:
                raise InvalidPack(
                    "directive %r takes %d or more argument(s), got %d at %s" % (kw, low, len(args), where), format=fmt
                )
        elif len(args) != want:
            raise InvalidPack(
                "directive %r takes %d argument(s), got %d at %s" % (kw, want, len(args), where), format=fmt
            )
        body = body_of()

        if kw in _SINGLE:
            if kw in seen:
                raise InvalidPack("duplicate directive %r at %s" % (kw, where), format=fmt)
            seen[kw] = args[0]
            if kw == "wire" and args[0] not in WIRES:
                raise InvalidPack("unknown wire %r (known: %s)" % (args[0], ", ".join(WIRES)), format=fmt)
            if kw == "bit_order" and args[0] not in BIT_ORDERS:
                raise bad("bit_order must be one of %s, got %r" % (", ".join(BIT_ORDERS), args[0]), lineno,
                          "bit_order")
        elif kw == "word_bytes":
            raise bad("'word_bytes' is not used in a sequential pack", lineno, "word_bytes")
        elif kw == "root":
            if root is not None:
                raise InvalidPack("duplicate directive 'root' at %s" % where, format=fmt)
            root = args[0]
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
            body.kinds.append(body.cur)
            body._index[args[0]] = body.cur
        elif kw == "requires":
            if in_block():
                raise bad("'requires' inside a block", lineno)
            if body.cur is None:
                raise bad("'requires' before the first 'kind'", lineno)
            for key in args:
                if key not in body.cur.requires:
                    body.cur.requires.append(key)
                key_refs.append((key, None))
        elif kw == "default":
            d = Default(args[0], args[1], lineno)
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
            f = make_field(args[0], args[1], args[2], args[3:], lineno)
            if f is not None:
                nd = Node(T_FIELD, lineno, length=f.loc[2], bindings=[f], locator=args[1])
                target(body).append(nd)
                last_field = (nd, args[2], args[3:])
        elif kw == "also":
            if prev_field is None:  # ruling 13 of 0.8
                raise bad("'also' is written directly after a 'field' line", lineno)
            nd, tname, options = prev_field
            carried = [o for o in options if o in ("swap", "trim")]
            extra = args[1:]
            if extra and (len(extra) != 2 or extra[0] != "via"):
                raise bad("an 'also' line is: also <key> [via <table>]", lineno)
            f = make_field(args[0], nd.locator, tname, carried + extra, lineno)
            if f is not None:
                nd.bindings.append(f)
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
            if not name or "." in name or name[0] == "!":
                raise bad("bad presence name %r" % name, lineno)
            if in_block():
                table = stack[-1].presences
                twice = name in table
            else:
                table = body.cur.presences if body.cur is not None else body.presences
                twice = name in body.presences or name in table
            if twice:  # ruling 12 of 0.8
                raise bad("presence %r is declared twice in one block" % name, lineno)
            length = None if args[1] == "fx" else bit_len(args[1], "a 'presence'", lineno)
            nd = Node(T_PRESENCE, lineno, name=name, length=length, named=set(), locator=args[1])
            table[name] = nd
            target(body).append(nd)
        elif kw == "if":
            conds = []
            for term in args:
                neg = term.startswith("!")
                name, dot, bit = (term[1:] if neg else term).rpartition(".")
                n = _uint(bit)
                if not dot or not name or n is None or n < 1:
                    raise bad("an 'if' term is [!]<name>.<i> with i from 1, got %r" % term, lineno)
                p = find_presence(name)
                if p is None:
                    raise bad("'if' names presence %r, which is not visible here" % name, lineno)
                if p.length is not None and n > p.length:
                    raise bad("'if' names bit %d of presence %r, which has %d bit(s)" % (n, name, p.length), lineno)
                p.named.add(n)
                conds.append((p, n, neg))
            nd = Node(T_IF, lineno, conds=conds, body=[])
            target(body).append(nd)
            stack.append(_Frame("if", nd, nd.body, lineno))
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
                        plus = int(args[i])
                    except ValueError:
                        raise bad("'plus' takes an integer, got %r" % args[i], lineno, args[0])
                else:
                    raise bad("a 'length' line is: length <len> [bytes|bits] [plus <n>] [whole]; got %r" % opt,
                              lineno, args[0])
                i += 1
            if whole:
                if unit == "bits":  # ruling 8 of 0.8
                    raise bad("'length ... bits whole': the whole message is counted in bytes", lineno, args[0])
                target(body).append(Node(T_WHOLE, lineno, length=length, plus=plus or 0, locator=args[0]))
            else:
                nd = Node(T_LENGTH, lineno, length=length, unit=unit or "bytes", plus=plus or 0, body=[],
                          locator=args[0])
                target(body).append(nd)
                stack.append(_Frame("length", nd, nd.body, lineno))
        elif kw == "group":
            name, mode = args[0], args[1]
            if ":" in mode:
                raise bad("a positional group locator (%r) in a sequential pack" % mode, lineno, mode)
            if mode not in GROUP_MODES or len(args) != (3 if mode == "count" else 2):
                raise bad("a sequential 'group' line is: group <list> count <len> | while | more | fx", lineno, mode)
            length = bit_len(args[2], "a group 'count'", lineno) if mode == "count" else None
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
            lists = body.cur.lists if body.cur is not None else body.lists
            for table in ((body.lists, lists) if lists is not body.lists else (lists,)):
                for other in table.get(name, ()):
                    if not blocks or not other or blocks[0] in other or other[0] in blocks:
                        raise bad("list %s is mapped by two groups of one scope that are not in different "
                                  "'if' or 'opt' blocks" % name, lineno, mode)
            lists.setdefault(name, []).append(blocks)
            nd = GroupNode(T_GROUP, lineno, list=name, mode=mode, length=length, scope=Body(), locator=mode)
            target(body).append(nd)
            key_refs.append((name, nd))
            stack.append(_Frame("group", nd, None, lineno, body=nd.scope))
        else:  # end
            if not stack:
                raise bad("'end' with no open block", lineno)
            fr = stack.pop()
            if fr.what == "group":
                close_body(fr.body, lineno)
            elif fr.what == "chain":
                parts = fr.node.parts
                if len(parts) > 1 and not parts[-1]:  # the chain ends with an `fx` line
                    parts.pop()
                    fr.node.ends_fx = True

    name = seen.get("format")
    for fr in stack:
        deferred.append((fr.lineno, bad("%r with no 'end'" % fr.what, fr.lineno)))
        if fr.what == "group":
            fr.body.close()
    close_body(msg, 0)
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
    if distribution not in DISTRIBUTIONS:
        raise InvalidPack(
            "unknown distribution %r (known: %s)" % (distribution, ", ".join(DISTRIBUTIONS)), format=name
        )
    if not name:
        raise InvalidPack("missing 'format' directive in %s" % source)
    version = seen.get("version")
    if not version:
        raise InvalidPack("missing 'version' directive in %s" % source, format=name)
    if root:
        raise InvalidPack("'root' is xml only", format=name, locator="root")

    pack = Pack(
        name=name, version=version, wire="bits", bit_order=seen.get("bit_order", "lsb"), kinds=msg.kinds,
        key_refs=key_refs, distribution=distribution, sources=sources, uses=uses, tables=tables,
    )
    pack.layout = "sequential"
    pack.seq = msg
    for lineno, (native, cot) in typemap:
        if native in pack.native_to_cot or cot in pack.cot_to_native:
            raise InvalidPack(
                "typemap %r <-> %r is not one-to-one at %s:%d" % (native, cot, source, lineno), format=name
            )
        pack.native_to_cot[native] = cot
        pack.cot_to_native[cot] = native
    return pack
