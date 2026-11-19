"""The `xml` wire: a small XML reader and a canonical writer, both written here.

Reader: tolerates a BOM, an `<?xml ...?>` prolog, processing instructions,
comments and whitespace outside the root; resolves the five predefined entities
and numeric character references; reads CDATA as text. DOCTYPE is refused.

A locator binds to the first sibling of each name on its path. Residue is the
unmapped attributes of the root element; unmapped child elements are not
carried (spec 0.1.1 rulings 6 and 7).

Writer (spec "Canonicalisation"): attributes in the order given (field lines,
then residue), child elements in first-appearance order, one space between
attributes, `/>` for an empty element, no prolog, UTF-8.
"""

import re

from .errors import MalformedWire, MissingRoot
from .packdef import parse_xml_locator

_NAME_STOP = set(" \t\r\n/>=<\"'")
_CHARREF = re.compile(r"&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);")
_PREDEF = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
MAX_DEPTH = 64  # ruling 35 of 0.8: elements, the root element is level 1


def _is_char(n):
    """Whether `n` is a character an XML document may hold (XML 1.0, Char)."""
    return (
        0x20 <= n <= 0xD7FF or n in (0x9, 0xA, 0xD) or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF
    )


_NAME = r'''[^ \t\r\n/>=<"']+'''
_WS = r"[ \t\r\n]*"
_START = re.compile("<(" + _NAME + ")")
_ATTR = re.compile(_WS + "(" + _NAME + ")" + _WS + "=" + _WS + r'''(?:"([^"]*)"|'([^']*)')''')
_START_END = re.compile(_WS + "(/?)>")
_CLOSE = re.compile("</(" + _NAME + ")" + _WS + ">")


class _El:
    __slots__ = ("name", "attrs", "children", "text", "index")

    def __init__(self, name):
        self.name = name
        self.attrs = []
        self.children = []
        self.text = []
        self.index = None

    def named(self):
        """-> {name: the child elements of that name, in document order}."""
        index = self.index
        if index is None:
            index = self.index = {}
            for child in self.children:
                same = index.get(child.name)
                if same is None:
                    index[child.name] = [child]
                else:
                    same.append(child)
        return index


class _Reader:
    def __init__(self, s, fmt):
        self.s = s
        self.i = 0
        self.fmt = fmt

    def fail(self, msg):
        raise MalformedWire("%s at offset %d" % (msg, self.i), format=self.fmt)

    def startswith(self, t):
        return self.s.startswith(t, self.i)

    def skip_ws(self):
        s, n = self.s, len(self.s)
        while self.i < n and s[self.i] in " \t\r\n":
            self.i += 1

    def skip_until(self, end):
        j = self.s.find(end, self.i)
        if j < 0:
            self.fail("unterminated construct, expected %r" % end)
        out = self.s[self.i:j]
        self.i = j + len(end)
        return out

    def skip_misc(self):
        while True:
            self.skip_ws()
            if self.startswith("<?"):
                self.i += 2
                self.skip_until("?>")
            elif self.startswith("<!--"):
                self.i += 4
                self.skip_until("-->")
            elif self.startswith("<!"):
                # a document type declaration is refused, so no entity is ever
                # declared, expanded or fetched; CDATA and any other
                # declaration have no place outside the root element
                self.fail("a markup declaration outside the root element is not supported")
            else:
                return

    def name(self):
        j = self.i
        s, n = self.s, len(self.s)
        while j < n and s[j] not in _NAME_STOP:
            j += 1
        if j == self.i:
            self.fail("expected a name")
        out = s[self.i:j]
        self.i = j
        return out

    def unescape(self, raw):
        if "&" not in raw:
            return raw

        def rep(m):
            ref = m.group(1)
            if ref.startswith("#"):
                # at most eight digits are read, so the number stays small
                digits = ref[2:] if ref.startswith("#x") else ref[1:]
                n = int(digits, 16 if ref.startswith("#x") else 10) if len(digits.lstrip("0")) <= 8 else -1
                if not _is_char(n):
                    self.fail("character reference &%s; names no XML character" % ref[:16])
                return chr(n)
            if ref in _PREDEF:
                return _PREDEF[ref]
            self.fail("unknown entity &%s;" % ref)

        out = _CHARREF.sub(rep, raw)
        if "&" in _CHARREF.sub("", raw):
            self.fail("bare '&' in content")
        return out

    def document(self):
        if self.s.startswith("﻿"):
            self.i = 1
        self.skip_misc()
        if not self.startswith("<"):
            self.fail("expected the root element")
        root = self.element(1)
        self.skip_misc()
        if self.i != len(self.s):
            self.fail("content after the root element")
        return root

    def element(self, level):
        """Read the element that starts at `self.i`. The recursion is as deep
        as the nesting, which `level` keeps to MAX_DEPTH."""
        s = self.s
        if level > MAX_DEPTH:
            self.fail("elements nested deeper than %d levels" % MAX_DEPTH)
        m = _START.match(s, self.i)
        if m is None or m.group(1)[0] in "!?&":
            self.i += 1
            self.fail("expected a name")
        el = _El(m.group(1))
        i = m.end()
        seen_names = None
        while True:
            m = _ATTR.match(s, i)
            if m is None:
                break
            self.i = i
            aname, dq, sq = m.groups()
            raw = dq if dq is not None else sq
            if "<" in raw:
                self.fail("'<' in attribute %r" % aname)
            if len(el.attrs) < 8:
                dup = any(seen == aname for seen, _ in el.attrs)
            else:
                if seen_names is None:
                    seen_names = {seen for seen, _ in el.attrs}
                dup = aname in seen_names
                seen_names.add(aname)
            if dup:
                self.fail("duplicate attribute %r" % aname[:40])
            el.attrs.append((aname, self.unescape(raw) if "&" in raw else raw))
            i = m.end()
        m = _START_END.match(s, i)
        if m is None:
            self.i = i
            self.fail("malformed start tag <%s" % el.name)
        i = m.end()
        if m.group(1):
            self.i = i
            return el
        text = el.text
        while True:
            j = s.find("<", i)
            if j < 0:
                self.i = len(s)
                self.fail("unclosed element <%s>" % el.name)
            if j > i:
                self.i = i
                raw = s[i:j]
                text.append(self.unescape(raw) if "&" in raw else raw)
            self.i = j
            c = s[j + 1:j + 2]
            if c == "/":
                m = _CLOSE.match(s, j)
                if m is None:
                    self.fail("malformed end tag")
                if m.group(1) != el.name:
                    self.fail("mismatched </%s> for <%s>" % (m.group(1), el.name))
                self.i = m.end()
                return el
            if c == "!":
                if s.startswith("<!--", j):
                    self.i = j + 4
                    self.skip_until("-->")
                elif s.startswith("<![CDATA[", j):
                    self.i = j + 9
                    text.append(self.skip_until("]]>"))
                else:
                    self.fail("unsupported markup declaration")
            elif c == "?":
                self.i = j + 2
                self.skip_until("?>")
            else:
                el.children.append(self.element(level + 1))
            i = self.i


def parse(pack, data):
    """-> the root element. The engine checks its name against the pack's
    `root`, or picks the kinds whose `root` it is (0.6, a root per kind)."""
    return _Reader(data, pack.name).document()


def check_root(pack, root):
    if root.name != pack.root:
        raise MissingRoot(
            "root element is <%s>, pack needs <%s>" % (root.name, pack.root),
            format=pack.name,
            locator=pack.root,
        )


def _find(root, path):
    """The element at `path` under the root, binding each name to its first
    sibling of that name; None when there is none."""
    el = root
    for seg in path:
        same = el.named().get(seg) if el.children else None
        if same is None:
            return None
        el = same[0]
    return el


def _attr(el, name):
    for k, v in el.attrs:
        if k == name:
            return v
    return None


def get(pack, root, field):
    """The wire text of one field, or None when it is absent: an attribute's
    value, or an element's text when it is not blank."""
    path, kind, name = field.loc
    el = _find(root, path)
    if el is None:
        return None
    if kind == "attr":
        return _attr(el, name)
    text = "".join(el.text)
    return text if text.strip() != "" else None


def items(pack, el, group):
    """-> the item elements of `group`: each child element named by the last
    name of its path, under the first element matched by the names before it,
    in document order; None when none occurs."""
    path = group.loc
    parent = _find(el, path[:-1])
    if parent is None:
        return None
    return parent.named().get(path[-1]) if parent.children else None


def when_holds(pack, root, when):
    """Text: the attribute's value or the element's text equals it. `present`:
    the attribute or the element exists."""
    path, kind, name = when.loc
    el = _find(root, path)
    if el is None:
        return False
    if kind == "attr":
        value = _attr(el, name)
        if value is None:
            return False
    else:
        value = "".join(el.text)
    return True if when.present else value == when.value


def residue(pack, root, mapped):
    """Unmapped attributes of the root element, in document order (0.1.1 ruling
    6: unmapped child elements are not carried)."""
    out = {}
    for name, value in root.attrs:
        loc = "@" + name
        if loc not in mapped:
            out[loc] = value
    return out


class _Node:
    __slots__ = ("name", "attrs", "children", "index", "text")

    def __init__(self, name):
        self.name = name
        self.attrs = {}
        self.children = []
        self.index = {}
        self.text = None

    def child(self, seg):
        node = self.index.get(seg)
        if node is None:
            name = seg.split("[", 1)[0] if seg.endswith("]") else seg
            node = _Node(name)
            self.index[seg] = node
            self.children.append(node)
        return node


def _fill(node, enc):
    """Write one encoded scope into `node`. A locator is written once, at the
    position of the first line that addresses it; its value is a `when` or
    `const` value if such a line names it, otherwise that of the earliest line
    whose key has a value. A group writes one element per item at its
    position; an empty list writes nothing."""
    plan = enc.plan
    first = plan.cache.get("first")
    if first is None:
        first = plan.cache["first"] = {}
        for ln in plan.lines:
            if (ln.is_field or not ln.present) and ln.locator not in first:
                first[ln.locator] = ln
    fixed = plan.cache.get("fixed")
    if fixed is None:  # the `when` and `const` values, the first for each locator
        fixed = plan.cache["fixed"] = {}
        for ln in plan.lines:
            if not (ln.is_field or ln.is_group or ln.present):
                fixed.setdefault(ln.locator, ln.value)
    resolved = dict(fixed)
    values = enc.values
    for i, ln in plan.ifields:
        if i in values and ln.locator not in resolved:
            resolved[ln.locator] = values[i]
    for i, g in plan.igroups:
        children = enc.groups.get(i)
        if children and g.locator not in resolved:
            resolved[g.locator] = children
    for locator in plan.order:
        if locator not in resolved:
            continue
        ln = first[locator]
        value = resolved[locator]
        if type(value) is list:
            path = ln.loc if ln.is_group else ln.loc[0]
            parent = node
            for seg in path[:-1]:
                parent = parent.child(seg)
            for child in value:
                item = _Node(path[-1])
                parent.children.append(item)
                _fill(item, child)
            continue
        path, kind, name = ln.loc
        target = node
        for seg in path:
            target = target.child(seg)
        if kind == "attr":
            target.attrs.setdefault(name, value)
        elif target.text is None:
            target.text = value


def write(pack, enc, residue=(), root_name=None):
    """`enc` is the encoded top scope; `residue` is (locator, text) in input
    order, written after the mapped values (the first value for a locator
    wins). `root_name` is the selected kind's root when it has its own.
    Returns the canonical document as str."""
    root = _Node(root_name or pack.root)
    _fill(root, enc)
    for locator, text in residue:
        try:
            path, kind, name = parse_xml_locator(locator, allow_presence=True)
        except ValueError:
            continue
        node = root
        for seg in path:
            node = node.child(seg)
        if kind == "attr":
            node.attrs.setdefault(name, text)
        elif kind == "text":
            if node.text is None:
                node.text = text
    out = []
    _ser(root, out)
    return "".join(out)


def _esc_attr(v):
    return (
        v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        .replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")
    )


def _esc_text(v):
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _ser(node, out):
    out.append("<" + node.name)
    for k, v in node.attrs.items():
        out.append(' %s="%s"' % (k, _esc_attr(v)))
    if not node.children and not node.text:
        out.append("/>")
        return
    out.append(">")
    if node.text:
        out.append(_esc_text(node.text))
    for c in node.children:
        _ser(c, out)
    out.append("</%s>" % node.name)
