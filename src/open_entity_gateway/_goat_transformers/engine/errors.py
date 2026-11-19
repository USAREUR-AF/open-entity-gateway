"""Named errors. Every failure carries the format and locator involved."""


class GTError(Exception):
    """Base of every goat-transformers error."""

    code = "error"

    def __init__(self, message, *, format=None, locator=None, detail=None):
        self.message = message
        self.format = format
        self.locator = locator
        self.detail = detail
        text = "%s: %s" % (self.code, message)
        if format is not None:
            text += " [format=%s]" % format
        if locator is not None:
            text += " [locator=%s]" % locator
        if detail:
            text += " [detail=%s]" % (detail,)
        super().__init__(text)


class UnknownDirective(GTError):
    code = "unknown-directive"


class UnknownTransform(GTError):
    code = "unknown-transform"


class InvalidPack(GTError):
    """A pack.def that is syntactically or structurally wrong (arity, wire, locator, duplicates)."""

    code = "invalid-pack"


class InvalidModel(GTError):
    """A model module file that is syntactically or structurally wrong."""

    code = "invalid-model"


# Version 0.4 load checks (spec/engine-spec.md, "Loading"). The code is the
# spec's error kind and the message begins with it.
class MissingDistribution(GTError):
    code = "missing-distribution"


class MissingSource(GTError):
    code = "missing-source"


class DistributionRefused(GTError):
    code = "distribution-refused"


class UnknownModule(GTError):
    code = "unknown-module"


class DistributionViolation(GTError):
    code = "distribution-violation"


class ModuleNotUsed(GTError):
    code = "module-not-used"


class UndeclaredKey(GTError):
    code = "undeclared-key"


class KeyTypeMismatch(GTError):
    code = "key-type-mismatch"


class DuplicateModule(GTError):
    code = "duplicate-module"


class DuplicateKey(GTError):
    code = "duplicate-key"


# Version 0.5 (spec/engine-spec.md, "Version 0.5: message kinds and the `json` wire").
class NoKind(GTError):
    """No kind of the format applies: a common `when` fails or no kind's `when`
    lines all hold (decode); no kind's `requires` keys are all present (encode)."""

    code = "no-kind"


class DuplicateKind(GTError):
    code = "duplicate-kind"


class DuplicateField(GTError):
    """Withdrawn in version 0.6 (a model key may be mapped by several `field`
    lines in any pack). Kept as a name; the engine no longer raises it."""

    code = "duplicate-field"


class BadDirective(GTError):
    """A directive that is known but not allowed where or how it is written:
    `requires` before the first `kind`, `when` in a pack with no `kind`,
    `bit_order` off the bits wire, a bit length its transform does not allow, a
    bits `when` value that does not fit its length; from 0.6 also `via` naming a
    table with no `map` line, a field option on a wire or transform it does not
    apply to, a `root` inside a kind off the xml wire, and a `derive` line whose
    function, argument count, keys or types do not fit; from 0.7 a `group` with
    no `end`, an `end` with no open group, `when`, `requires`, `root`, `kind`,
    `map` or `typemap` inside a group, `count` off the bits wire, a bits group
    with no `count`, a line at or beyond the start of a group counted by a
    locator, a line of an item beyond `item <bits>`, a list mapped by two
    groups of one scope, `.` outside a group, and an unknown or repeated
    field option."""

    code = "bad-directive"


class UnknownFormat(GTError):
    code = "unknown-format"


class MalformedWire(GTError):
    code = "malformed-wire"


class MissingRoot(GTError):
    code = "missing-root"


class TransformDecodeFailure(GTError):
    """One or more fields could not be decoded. `entity` holds the fields that did
    decode; `absent` lists (model_key, locator, reason) for each that did not."""

    code = "transform-decode"

    def __init__(self, message, *, format=None, locator=None, entity=None, absent=()):
        self.entity = entity
        self.absent = list(absent)
        super().__init__(
            message,
            format=format,
            locator=locator,
            detail=", ".join("%s@%s: %s" % a for a in self.absent) or None,
        )


class TransformEncodeFailure(GTError):
    code = "transform-encode"
