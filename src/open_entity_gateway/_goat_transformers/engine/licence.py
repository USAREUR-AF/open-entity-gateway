"""Licence profiles in the loaders (spec/engine-spec.md, Version 0.29).

The engine reads three kinds of file, by exact line forms and without a TOML
reader (ruling 1): the licence table `licences.toml` beside the packs
directory, the licence of each pack (`<packs>/<d>/REUSE.toml`) and of each
model module (`<models>/<m>.def.license`). From them every module and pack
has a profile (ruling 2), and a loader given a profile loads only what that
profile holds (ruling 3). Every failure to read one of these files as
ruling 1 says is `bad-licence`."""

import errno
import os
import re

from .errors import BadLicence

# The profiles in their order (open < open+copyleft < everything < none).
PROFILES = ("open", "open+copyleft", "everything")
NONE = len(PROFILES)  # the rank of "in no profile"


def rank_name(rank):
    return PROFILES[rank] if rank < NONE else "none"


def check_profile(profile):
    """-> the rank of a profile name; `bad-licence` for a name not one of the
    three (ruling 3a: in a library, before anything is read)."""
    if profile not in PROFILES:
        raise BadLicence("unknown profile %r (profiles: %s)" % (profile, ", ".join(PROFILES)))
    return PROFILES.index(profile)


_ID = r"[A-Za-z0-9.+-]+"
_NAME = r"[A-Za-z0-9_-]+"
# REUSE-IgnoreStart: the quoted licence lines below are data
_REUSE_LINE = re.compile(r"SPDX-License-Identifier[ \t]*=[ \t]*\"(" + _ID + r")\"")
_MODULE_LICENCE_LINE = re.compile(r"SPDX-License-Identifier:[ \t]+(" + _ID + r")")
_ROW = re.compile(r"\[licences\.\"(" + _ID + r")\"\]")
_KEYS = {
    "commercial": re.compile(r"commercial[ \t]*=[ \t]*\"(yes|no|unknown)\""),
    "share_alike": re.compile(r"share_alike[ \t]*=[ \t]*(true|false)"),
    "field_of_use": re.compile(r"field_of_use[ \t]*=[ \t]*(\[\]|\[\".*\"\])", re.S),
    "distribution": re.compile(r"distribution[ \t]*=[ \t]*\"(public|controlled)\""),
}
_SKIPPED = ("name", "url", "attribution")
_MODULE = re.compile(r"module[ \t]+(" + _NAME + r")")
_USES = re.compile(r"uses[ \t]+(" + _NAME + r")")

# The table when `licences.toml` does not exist (ruling 1c).
DEFAULT_TABLE = {"Apache-2.0": {"commercial": "yes", "share_alike": "false", "field_of_use": "[]",
                                "distribution": "public"}}
DEFAULT_LICENCE = "Apache-2.0"


def _split(text):
    """Ruling 1, Lines: cut at each line feed, one carriage return before it
    (or at the end of the file) removed, each line trimmed of spaces and tabs.
    A byte-order mark is kept."""
    out = []
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        out.append(line.strip(" \t"))
    return out


def read_lines(path):
    """-> the trimmed lines of a licence file, or None when it does not exist
    (no entry of that name, or a part of the path before the last is not a
    directory or does not exist). Any other failure to read it, and text
    that is not UTF-8, is `bad-licence`."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise BadLicence("cannot read licence file %s: %s" % (path, e.strerror or e))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BadLicence("licence file %s is not UTF-8" % path)
    return _split(text)


def _scan_lines(path):
    """Ruling 2b: the trimmed lines of a module file or `pack.def`; a file
    that cannot be read or is not UTF-8 adds no line (never `bad-licence`)."""
    try:
        with open(path, "rb") as fh:
            return _split(fh.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def module_and_uses(path):
    """-> (the name of the first `module` line or None, the `uses` names)."""
    name = None
    uses = []
    for line in _scan_lines(path):
        m = _MODULE.fullmatch(line)
        if m is not None:
            if name is None:
                name = m.group(1)
            continue
        m = _USES.fullmatch(line)
        if m is not None:
            uses.append(m.group(1))
    return name, uses


def _one_licence(path, line_re, prefix, bad_after):
    """1a and 1b: exactly one licence line; a line that begins with `prefix`
    followed by one of `bad_after` (or with `prefix` alone when `bad_after`
    is empty) and is not a licence line is `bad-licence`. No file: Apache-2.0."""
    lines = read_lines(path)
    if lines is None:
        return DEFAULT_LICENCE
    found = []
    for i, line in enumerate(lines, 1):
        m = line_re.fullmatch(line)
        if m is not None:
            found.append(m.group(1))
            continue
        if line.startswith(prefix) and (not bad_after or line[len(prefix):len(prefix) + 1] in bad_after):
            raise BadLicence("%s line %d: not a licence line: %r" % (path, i, line[:80]))
    if len(found) != 1:
        raise BadLicence("%s holds %d licence lines, not exactly one" % (path, len(found)))
    return found[0]


def pack_licence(pack_dir):
    """Ruling 1a: the licence of a pack, from `<pack_dir>/REUSE.toml`."""
    return _one_licence(os.path.join(pack_dir, "REUSE.toml"), _REUSE_LINE, "SPDX-License-Identifier",
                        (" ", "\t", "="))


def module_licence(module_path):
    """Ruling 1b: the licence of a module, from `<module file>.license`."""
    return _one_licence(module_path + ".license", _MODULE_LICENCE_LINE, "SPDX-License-Identifier:", ())
    # REUSE-IgnoreEnd


def read_table(path):
    """Ruling 1c: `licences.toml` -> {licence id: {key: value}}."""
    lines = read_lines(path)
    if lines is None:
        return dict(DEFAULT_TABLE)
    table = {}
    row = None
    rid = None
    skipped = None

    def close():
        if row is not None:
            for key in _KEYS:
                if key not in row:
                    raise BadLicence("%s: the row of %s has no %s line" % (path, rid, key))

    for i, line in enumerate(lines, 1):
        if line == "" or line.startswith("#"):
            continue
        m = _ROW.fullmatch(line)
        if m is not None:
            close()
            rid = m.group(1)
            if rid in table:
                raise BadLicence("%s line %d: a second row of %s" % (path, i, rid))
            row = {}
            skipped = set()
            table[rid] = row
            continue
        if row is None:
            raise BadLicence("%s line %d: a line before the first row: %r" % (path, i, line[:80]))
        for key, rx in _KEYS.items():
            m = rx.fullmatch(line)
            if m is not None:
                if key in row:
                    raise BadLicence("%s line %d: %s given twice in the row of %s" % (path, i, key, rid))
                row[key] = m.group(1)
                break
        else:
            for word in _SKIPPED:
                if line.startswith(word) and line[len(word):len(word) + 1] in (" ", "\t", "="):
                    if word in skipped:
                        raise BadLicence("%s line %d: %s given twice in the row of %s" % (path, i, word, rid))
                    skipped.add(word)
                    break
            else:
                raise BadLicence("%s line %d: not a line of the licence table: %r" % (path, i, line[:80]))
    close()
    return table


def licence_rank(table, licence):
    """Ruling 2a."""
    row = table.get(licence)
    if row is None:
        return PROFILES.index("everything")
    if row["distribution"] == "controlled":
        return NONE
    if row["commercial"] == "yes" and row["field_of_use"] == "[]":
        return 1 if row["share_alike"] == "true" else 0
    return 2


def module_file_names(models_dir):
    """The module files of Version 0.23, Loading, 1: `core.def` first, then
    every other name ending `.def` that is a regular file or a link to one,
    in byte order of name. A directory that does not exist or is not one
    has none."""
    if not os.path.isdir(models_dir):
        return []
    return sorted(
        (n for n in os.listdir(models_dir) if n.endswith(".def") and os.path.isfile(os.path.join(models_dir, n))),
        key=lambda n: (n != "core.def", os.fsencode(n)),
    )


class Licences:
    """The licence facts of one load (ruling 3b): read with `read`, then asked
    for each pack as it is reached (ruling 3d)."""

    def __init__(self, packs_dir, models_dir):
        self.packs_dir = packs_dir
        self.models_dir = models_dir
        self.table = {}
        self.module_files = []          # file names, in loading order
        self.module_rank = {}           # file name -> rank (2b)
        self.name_rank = {}             # module name -> widest rank of the files of that name

    def read(self):
        """Ruling 3b: `licences.toml`, then each module file's licence and its
        `module` and `uses` lines, in loading order; then every module's
        profile. The first file that cannot be read is `bad-licence`."""
        self.table = read_table(self.packs_dir + "/../licences.toml")
        self.module_files = module_file_names(self.models_dir)
        own, names, uses = {}, {}, {}
        for n in self.module_files:
            path = os.path.join(self.models_dir, n)
            own[n] = licence_rank(self.table, module_licence(path))
            names[n], uses[n] = module_and_uses(path)
        rank = dict(own)
        changed = True
        while changed:  # the widest over `uses`, directly or through others (cycles included)
            changed = False
            by_name = {}
            for n in self.module_files:
                if names[n] is not None:
                    by_name[names[n]] = max(by_name.get(names[n], 0), rank[n])
            for n in self.module_files:
                for u in uses[n]:
                    if u in by_name and by_name[u] > rank[n]:
                        rank[n] = by_name[u]
                        changed = True
        self.module_rank = rank
        self.name_rank = {}
        for n in self.module_files:
            if names[n] is not None:
                self.name_rank[names[n]] = max(self.name_rank.get(names[n], 0), rank[n])
        return self

    def skipped_modules(self, profile_rank):
        """The module file names a profile does not hold (3c)."""
        return {n for n, r in self.module_rank.items() if r > profile_rank}

    def pack(self, pack_dir):
        """Ruling 3d and 2c: -> (licence, rank) of the pack in `pack_dir`."""
        licence = pack_licence(pack_dir)
        rank = licence_rank(self.table, licence)
        rank = max(rank, self.name_rank.get("core", 0))
        _, uses = module_and_uses(os.path.join(pack_dir, "pack.def"))
        for u in uses:
            rank = max(rank, self.name_rank.get(u, 0))
        return licence, rank


def json_text(value):
    """A value of the `licences --json` output (ruling 4c)."""
    out = []
    for c in value:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c < " ":
            out.append("\\u%04x" % ord(c))
        else:
            out.append(c)
    return "".join(out)
