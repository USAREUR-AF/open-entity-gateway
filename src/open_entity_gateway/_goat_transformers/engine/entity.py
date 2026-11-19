"""The shared model in memory (`Entity`, `Item`) and the value-table lookups
of a `via` field."""

from dataclasses import dataclass, field as dc_field

from .transforms import canonical_text, read_typed


def _canon(text, type_name):
    """Canonical text of a table entry read as `type_name`, or None when it
    cannot be read as that type (it then matches no value)."""
    try:
        return canonical_text(read_typed(text, type_name), type_name)
    except (ValueError, OverflowError):
        return None


def _build_via(f, table):
    """The two lookups of a `via` field: canonical native text -> model value,
    and canonical model text -> native text. The first entry for a value wins."""
    nt, kt = f.transform.yields, f.key_type
    f.via_dec, f.via_enc = {}, {}
    for native, model in table:
        n = _canon(native, nt)
        if n is not None and n not in f.via_dec:
            try:
                f.via_dec[n] = read_typed(model, kt)
            except (ValueError, OverflowError):
                pass
        m = _canon(model, kt)
        if m is not None and m not in f.via_enc:
            f.via_enc[m] = native


def _via_decode(f, value):
    """After the transform on decode: what the transform yields -> what the key holds."""
    nt = f.transform.yields
    text = canonical_text(value, nt)
    if text in f.via_dec:
        return f.via_dec[text]
    if nt == f.key_type:
        return value  # no entry and both sides one type: passes through
    raise ValueError("%s has no entry in table %s" % (text, f.via))


def _via_encode(f, value):
    """Before the transform on encode: what the key holds -> what the transform takes."""
    nt, kt = f.transform.yields, f.key_type
    try:
        text = canonical_text(value, kt)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%r is not a %s value" % (value, kt))
    native = f.via_enc.get(text)
    if native is None:
        if nt == kt:
            return value
        raise ValueError("%s has no entry in table %s" % (text, f.via))
    return read_typed(native, nt)


@dataclass
class Item:
    """One item of a list (0.7): model keys and lists exactly as an entity
    holds them, without residue. In a sequential pack a group body may hold
    kinds (0.8): the item then records the kind it selected."""

    fields: dict = dc_field(default_factory=dict)
    lists: dict = dc_field(default_factory=dict)
    kind: tuple = None  # 0.8: the pair `(format, kind name)` the item selected when it was decoded, or None

    def __getitem__(self, key):
        return self.fields[key]

    def get(self, key, default=None):
        return self.fields.get(key, default)


@dataclass
class Entity:
    """Shared model: named fields plus residue. `residue` maps a format name to an
    ordered {locator: original string value}; it never crosses formats. `kind`
    is the pair `(format, kind name)` a decode selected, or None. `lists` maps
    a list name to its items, in order (0.7); a list that is present and empty
    is distinct from an absent one."""

    fields: dict = dc_field(default_factory=dict)
    residue: dict = dc_field(default_factory=dict)
    kind: tuple = None
    lists: dict = dc_field(default_factory=dict)

    def __getitem__(self, key):
        return self.fields[key]

    def get(self, key, default=None):
        return self.fields.get(key, default)
