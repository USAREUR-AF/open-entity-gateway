#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Every requirement each distribution in <lib> declares, for this Python and machine, is in <lib>.

    /usr/bin/python3 -I packaging/requires_check.py <lib>

The archive is installed with --no-deps from lock files, so nothing else checks that the set
is complete. Requirements behind an `extra` are not needed and are skipped; other markers are
evaluated for the running interpreter (PEP 508 variables, version-aware comparisons).
"""

import platform
import re
import sys

lib = sys.argv[1]
sys.path[:] = [lib] + [p for p in sys.path if not p.endswith(("site-packages", "dist-packages"))]
from importlib import metadata  # noqa: E402

ENV = {
    "python_version": "{}.{}".format(*sys.version_info[:2]),
    "python_full_version": platform.python_version(),
    "implementation_name": sys.implementation.name,
    "platform_python_implementation": platform.python_implementation(),
    "platform_machine": platform.machine(),
    "platform_system": platform.system(),
    "sys_platform": sys.platform,
    "os_name": __import__("os").name,
    "platform_release": platform.release(),
    "implementation_version": platform.python_version(),
}
VERSIONED = {"python_version", "python_full_version", "implementation_version", "platform_release"}
CLAUSE = re.compile(r"""(\w+)\s*(===|==|!=|<=|>=|<|>|~=|not in|in)\s*(["'])(.*?)\3""")


def vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def clause(m: re.Match[str]) -> str:
    name, op, _, value = m.groups()
    have = ENV.get(name, "")
    if op in ("in", "not in"):
        r = (have in value) == (op == "in")
    elif name in VERSIONED:
        a, b = vt(have), vt(value)
        r = {
            "==": a == b,
            "===": have == value,
            "!=": a != b,
            "<": a < b,
            "<=": a <= b,
            ">": a > b,
            ">=": a >= b,
            "~=": a >= b and a[: len(b) - 1] == b[: len(b) - 1],
        }[op]
    else:
        r = {"==": have == value, "===": have == value, "!=": have != value}.get(op, False)
    return "True" if r else "False"


def needed(marker: str) -> bool:
    if re.search(r"\bextra\s*==", marker):
        return False
    expr = CLAUSE.sub(clause, marker)
    if not re.fullmatch(r"[\sTrueFalsandor()]*", expr):
        raise SystemExit(f"requires_check: cannot read the marker {marker!r}")
    return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - only True/False/and/or/not/() remain


def norm(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


have = {norm(d.metadata["Name"]) for d in metadata.distributions(path=[lib])}
missing = []
for d in metadata.distributions(path=[lib]):
    for req in d.requires or []:
        name_part, _, marker = req.partition(";")
        name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", name_part).group(1)
        if marker.strip() and not needed(marker.strip()):
            continue
        if norm(name) not in have:
            missing.append(f"{d.metadata['Name']} needs {req.strip()}")
if missing:
    print("requires_check: missing from lib/:\n  " + "\n  ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"requires_check: {len(have)} distributions, every requirement present")
