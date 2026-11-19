"""Model module parsing (grammar: spec/engine-spec.md, "Model module grammar")."""

import math
from dataclasses import dataclass, field as dc_field

from .errors import (
    BadDirective, DistributionRefused, DistributionViolation, DuplicateKey, DuplicateModule, InvalidModel,
    MissingDistribution, MissingSource, UnknownDirective, UnknownModule,
)
from .packdef import CORE, DISTRIBUTIONS, split_key, tokenize
from .transforms import parse_factor, read_typed

KEY_TYPES = ("string", "float", "int")
_SINGLE = ("module", "version", "distribution")


@dataclass
class Key:
    name: str
    type: str
    unit: str = None  # None when the file says `-`
    meaning: str = None


@dataclass
class Derive:
    """`derive <model-key> <function> <argument-key>...` (version 0.6): the
    target follows from the arguments. `fn` takes the argument values and
    returns the target's value, or None when the target stays absent."""

    target: str
    function: str
    args: tuple
    fn: object = None
    lineno: int = 0
    partial: bool = False  # `any` (0.8): `fn` is given the arguments that are present, at least one


@dataclass
class Module:
    name: str
    version: str
    distribution: str
    sources: list = dc_field(default_factory=list)
    keys: dict = dc_field(default_factory=dict)  # name -> Key, in file order
    path: str = None
    derives: list = dc_field(default_factory=list)  # Derive, in file order
    lists: dict = dc_field(default_factory=dict)  # list name -> meaning (0.7); shares the key namespace
    uses: list = dc_field(default_factory=list)  # modules named in `uses` lines (0.8)


_DEG = math.pi / 180.0  # degrees to radians, with pi as the double nearest to it
_RAD = 180.0 / math.pi  # radians to degrees: the quotient computed first (0.6 ruling 1)
_INT64 = (-(1 << 63), (1 << 63) - 1)


def _finite(x):
    return x if math.isfinite(x) else None


def _bearing(east, north):
    if east == 0 and north == 0:
        return None
    d = math.atan2(east, north) * _RAD
    if not math.isfinite(d):
        return None
    if d < 0:
        d += 360.0
    if d >= 360.0:
        d -= 360.0
    return d


def _parse_int(text):
    """Ruling 28 of 0.8: an optional `-` and then digits, leading zeros
    included, within 64 bits; anything else leaves the target absent."""
    digits = text[1:] if text.startswith("-") else text
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    if len(digits.lstrip("0")) > 19:
        return None
    value = int(text)
    return value if _INT64[0] <= value <= _INT64[1] else None


_NO_PARAM = ("copy", "hypot", "bearing", "east_of", "north_of", "neg", "day_seconds", "any", "not", "parse_int",
             "text_of")
# 0.8: function -> (argument count or None for one or more, argument type, result type)
_TYPED = {
    "day_seconds": (1, "int", "float"),
    "any": (None, "int", "int"),
    "not": (1, "int", "int"),
    "parse_int": (1, "string", "int"),
    "text_of": (1, "int", "string"),
}
_TYPED_FN = {
    "day_seconds": lambda a: (int(a) % 86400000) / 1000.0,  # seconds since UTC midnight of the instant
    "any": lambda *present: 1 if any(v != 0 for v in present) else 0,
    "not": lambda a: 1 if a == 0 else 0,
    "parse_int": _parse_int,
    "text_of": lambda a: str(int(a)),
}


# function -> argument count, None for one or more
_ARG_COUNT = {"copy": 1, "hypot": 2, "bearing": 2, "east_of": 2, "north_of": 2, "neg": 1, "day_seconds": 1,
              "any": None, "not": 1, "parse_int": 1, "text_of": 1, "scale": 1, "offset": 1, "join": None}


def _check_derive_text(function, n):
    """Ruling 32 of 0.8: what a `derive` line shows on its own text, the
    function name, its parameter and the number of arguments. Raises
    ValueError."""
    name, colon, param = function.partition(":")
    if name not in _ARG_COUNT:
        raise ValueError("unknown function %r" % function)
    if name in _NO_PARAM:
        if colon:
            raise ValueError("%s takes no parameter" % name)
    elif not colon:
        raise ValueError("%s needs a parameter" % name)
    elif name == "scale":
        parse_factor(param)
    elif name == "offset":
        try:
            read_typed(param, "int")
        except ValueError:
            read_typed(param, "float")
    count = _ARG_COUNT[name]
    if (n != count) if count is not None else (n < 1):
        raise ValueError("%s takes %s argument(s), got %d" % (name, count if count is not None else "1 or more", n))


def _build_derive(function, target_type, arg_types):
    """-> the function of one `derive` line, checked against the types of its
    target and arguments. Raises ValueError when the function name, the
    argument count or a type does not fit."""
    name, colon, param = function.partition(":")
    n = len(arg_types)

    def floats(count):
        if n != count:
            raise ValueError("%s takes %d argument(s), got %d" % (name, count, n))
        if target_type != "float" or any(t != "float" for t in arg_types):
            raise ValueError("%s needs a float target and float arguments" % name)

    if name in _NO_PARAM and colon:
        raise ValueError("%s takes no parameter" % name)
    if name in _TYPED:  # the functions added in 0.8
        count, arg_type, result_type = _TYPED[name]
        if (n != count) if count is not None else (n < 1):
            raise ValueError("%s takes %s argument(s), got %d" % (name, count if count is not None else "1 or more", n))
        if target_type != result_type or any(t != arg_type for t in arg_types):
            raise ValueError("%s needs %s argument(s) and a %s target" % (name, arg_type, result_type))
        return _TYPED_FN[name]
    if name == "copy":
        if n != 1:
            raise ValueError("copy takes 1 argument(s), got %d" % n)
        if arg_types[0] != target_type:
            raise ValueError("copy needs a target and an argument of one type")
        return lambda a: _finite(a) if isinstance(a, float) else a
    if name == "hypot":
        floats(2)
        return lambda a, b: _finite(math.sqrt(float(a) * float(a) + float(b) * float(b)))
    if name == "bearing":
        floats(2)
        return lambda east, north: _bearing(float(east), float(north))
    if name == "east_of":
        floats(2)
        return lambda speed, course: _finite(float(speed) * math.sin(float(course) * _DEG))
    if name == "north_of":
        floats(2)
        return lambda speed, course: _finite(float(speed) * math.cos(float(course) * _DEG))
    if name == "neg":
        floats(1)
        return lambda a: _finite(-float(a))
    if name == "scale" and colon:
        floats(1)
        factor, fraction = parse_factor(param)
        if fraction is not None:
            num, den = fraction
            return lambda a: _finite(float(a) * num / den)
        return lambda a: _finite(float(a) * factor)
    if name == "offset" and colon:
        if n != 1:
            raise ValueError("offset takes 1 argument(s), got %d" % n)
        if target_type == "int" and arg_types[0] == "int":
            k = read_typed(param, "int")  # 0.6 ruling 8: an integer `n`; an overflow leaves the target absent

            def add(a):
                r = int(a) + k
                return r if _INT64[0] <= r <= _INT64[1] else None

            return add
        if target_type == "float" and arg_types[0] == "float":
            x = read_typed(param, "float")
            return lambda a: _finite(float(a) + x)
        raise ValueError("offset needs a target and an argument that are both float or both int")
    if name == "join" and colon:
        if n < 1:  # 0.6 ruling 7
            raise ValueError("join takes 1 or more argument(s), got %d" % n)
        if target_type != "string" or any(t != "string" for t in arg_types):
            raise ValueError("join needs a string target and string arguments")
        return lambda *parts: param.join(parts)
    raise ValueError("unknown function %r" % function)


def _check_derives(module, lines, models, source):
    """Resolve every `derive` line. An argument is a key of the module itself,
    of `core` or of a module named in a `uses` line (0.8); the target is a key
    of the module itself or of `core`. Any misfit is `bad-directive`."""
    def key_type(key, where, target):
        m, k = split_key(key)
        if m == module.name:
            owner = module
        elif m == CORE or (not target and m in module.uses):
            owner = models.get(m)
        else:
            raise BadDirective(
                "derive %s %s is of module %s; a derive line takes its target from core or module %s, and its "
                "arguments also from the modules it names in 'uses' lines, at %s"
                % ("target" if target else "argument", key, m, module.name, where)
            )
        decl = owner.keys.get(k) if owner is not None else None
        if decl is None:  # a list is not a scalar key (ruling 18 of 0.7)
            raise BadDirective("derive names undeclared key %s at %s" % (key, where))
        return decl.type

    for lineno, args in lines:
        where = "%s:%d" % (source, lineno)
        target, function, arg_keys = args[0], args[1], tuple(args[2:])
        target_type = key_type(target, where, True)
        arg_types = [key_type(k, where, False) for k in arg_keys]
        try:
            fn = _build_derive(function, target_type, arg_types)
        except ValueError as e:
            raise BadDirective("derive %s %s at %s: %s" % (target, function, where, e))
        module.derives.append(Derive(target, function, arg_keys, fn, lineno, partial=function == "any"))


def scan_uses(text):
    """-> (module name or None, the modules its `uses` lines name), read
    without checking the file: what the directory loader needs to order the
    modules (0.8)."""
    name = None
    uses = []
    for raw in text.splitlines():
        line = raw.lstrip()
        if line.startswith("uses") or line.startswith("module"):
            try:
                toks = tokenize(raw, error=InvalidModel)
            except InvalidModel:
                continue
            if len(toks) == 2 and toks[0] == "uses" and toks[1] not in uses:
                uses.append(toks[1])
            elif len(toks) == 2 and toks[0] == "module" and name is None:
                name = toks[1]
    return name, uses


def parse_model_text(text, *, source="model.def", core=None, models=None, max_distribution=None):
    """Parse one module. Line errors are raised as the file is read,
    `duplicate-key` among them and a `derive` line refused on its own text
    (ruling 32 of 0.8). Then, in the order of ruling 29 of 0.8:
    `missing-distribution`, `missing-source`, `distribution-refused` (when
    `max_distribution`, the engine's limit, is given), `unknown-module` and
    `distribution-violation` (when `models`, the loaded modules by name, is
    given), the `derive` checks that need keys, `duplicate-module`. `core` is the loaded
    `core` module, for a caller that has no other module."""
    check_uses = models is not None
    loaded = models if models is not None else {}
    models = dict(models) if models else {}
    if core is not None:
        models.setdefault(CORE, core)
    uses = []
    seen = {}
    sources = []
    keys = {}
    lists = {}
    derives = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        toks = tokenize(raw, source=source, lineno=lineno, error=InvalidModel)
        if not toks:
            continue
        kw, args = toks[0], toks[1:]
        where = "%s:%d" % (source, lineno)
        if kw in _SINGLE or kw == "source":
            if len(args) != 1:
                raise InvalidModel("directive %r takes 1 argument(s), got %d at %s" % (kw, len(args), where))
            if kw == "source":
                sources.append(args[0])
            elif kw in seen:
                raise InvalidModel("duplicate directive %r at %s" % (kw, where))
            else:
                seen[kw] = args[0]
        elif kw == "key":
            if len(args) not in (3, 4):
                raise InvalidModel("directive 'key' takes 3 or 4 argument(s), got %d at %s" % (len(args), where))
            kname, ktype, unit = args[0], args[1], args[2]
            if "." in kname:
                raise InvalidModel("key name %r contains '.' at %s" % (kname, where))
            if ktype not in KEY_TYPES:
                raise InvalidModel(
                    "key %r has unknown type %r (known: %s) at %s" % (kname, ktype, ", ".join(KEY_TYPES), where)
                )
            if kname in keys or kname in lists:
                raise DuplicateKey("key %r appears twice in the module, at %s" % (kname, where))
            keys[kname] = Key(kname, ktype, None if unit == "-" else unit, args[3] if len(args) == 4 else None)
        elif kw == "list":
            if len(args) not in (1, 2):
                raise InvalidModel("directive 'list' takes 1 or 2 argument(s), got %d at %s" % (len(args), where))
            lname = args[0]
            if "." in lname:
                raise InvalidModel("list name %r contains '.' at %s" % (lname, where))
            if lname in keys or lname in lists:  # a list shares the key namespace of its module
                raise DuplicateKey("name %r appears twice in the module, at %s" % (lname, where))
            lists[lname] = args[1] if len(args) == 2 else None
        elif kw == "uses":
            if len(args) != 1:
                raise InvalidModel("directive 'uses' takes 1 argument(s), got %d at %s" % (len(args), where))
            if args[0] not in uses:
                uses.append(args[0])
        elif kw == "derive":
            if len(args) < 2:
                raise InvalidModel("directive 'derive' takes 2 or more argument(s), got %d at %s" % (len(args), where))
            try:  # ruling 32 of 0.8: refused on its own text, a line error
                _check_derive_text(args[1], len(args) - 2)
            except ValueError as e:
                raise BadDirective("derive %s %s at %s: %s" % (args[0], args[1], where, e))
            derives.append((lineno, args))
        else:
            raise UnknownDirective("unknown directive %r at %s" % (kw, where))

    name = seen.get("module")
    module = Module(name=name, version=seen.get("version"), distribution=seen.get("distribution"), sources=sources,
                    keys=keys, lists=lists, uses=uses)
    if module.distribution is None:
        raise MissingDistribution("module %r has no 'distribution' line in %s" % (name, source))
    if not sources:
        raise MissingSource("module %r has no 'source' line in %s" % (name, source))
    if not name:
        raise InvalidModel("missing 'module' directive in %s" % source)
    if "." in name:
        raise InvalidModel("module name %r contains '.'" % name)
    if not module.version:
        raise InvalidModel("missing 'version' directive in %s (module %s)" % (source, name))
    if module.distribution not in DISTRIBUTIONS:
        raise InvalidModel(
            "unknown distribution %r (known: %s) in module %s" % (module.distribution, ", ".join(DISTRIBUTIONS), name)
        )
    if max_distribution is not None and (
        DISTRIBUTIONS.index(module.distribution) > DISTRIBUTIONS.index(max_distribution)
    ):
        raise DistributionRefused(
            "module %s is %s; this engine's limit is %s" % (name, module.distribution, max_distribution)
        )
    if check_uses:
        for m in uses:
            if m not in models and m != name:
                raise UnknownModule("module %r uses module %s, which is not loaded (%s)" % (name, m, source))
        if module.distribution == "public":  # a public module uses only public modules
            for m in uses:
                if m != name and models[m].distribution != "public":
                    raise DistributionViolation(
                        "public module %s uses %s module %s" % (name, models[m].distribution, m)
                    )
    _check_derives(module, derives, models, source)
    if name in loaded:
        raise DuplicateModule("module %s loaded twice (%s, %s)" % (name, loaded[name].path, source))
    return module


def load_model_file(path, core=None, models=None, max_distribution=None):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    module = parse_model_text(text, source=path, core=core, models=models, max_distribution=max_distribution)
    module.path = path
    return module
