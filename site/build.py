#!/usr/bin/env python3
"""Prepare site/ for publishing and check its numbers.

    python3 site/build.py        # with open-entity-gateway and vendors/lattice installed

1. Runs `open-entity-gateway print` on the example weather-station records with
   the repository's mappings, for the Lattice, CoT and ASTERIX CAT062 adapters,
   and writes what it printed, the input record and the mapping file to
   site/assets/from-tree/hero.json (git-ignored). The hero shows only that.
2. Copies the two figures in docs/img/ beside it.
3. Measures the numbers on the page from the tree and fails if site/index.html
   states anything else, or if the CoT event the page shows without JavaScript
   is not the one the gateway prints.
4. Runs site/fitcheck.py: in headless Chrome at 1440 and 390 px, no command box
   is cut off, and no output box at 1440 px.
"""

import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
OUT = os.path.join(SITE, "assets", "from-tree")
RECORDS = "examples/records/weather-station.records.jsonl"
MAPPING = "mappings/weather-station.position.yml"
ADAPTERS = ["lattice", "cot", "asterix-cat062"]

# CAT062 data items in the order of the UAP (FRN 1..), with their lengths in octets;
# None marks a repetitive or variable item handled below.
UAP = [
    "010",
    None,
    "015",
    "070",
    "105",
    "100",
    "185",
    "210",
    "060",
    "245",
    "380",
    "040",
    "080",
    "290",
    "200",
    "295",
    "136",
    "130",
    "135",
    "220",
]
FIXED = {
    "010": 2,
    "015": 1,
    "070": 3,
    "105": 8,
    "100": 6,
    "185": 4,
    "210": 2,
    "040": 2,
    "136": 2,
    "130": 2,
    "135": 2,
    "200": 1,
}


def gateway_print(adapter):
    cmd = [sys.executable, "-m", "open_entity_gateway", "print", "--mapping", "mappings", "--adapter", adapter, RECORDS]
    # The command is fixed here; only the adapter name varies, from ADAPTERS.
    out = subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip().splitlines()  # noqa: S603
    if len(out) != 1:
        sys.exit(f"site/build.py: expected one {adapter} payload from {RECORDS}, got {len(out)}")
    return out[0]


def split_cat062(hexstr):
    """Split one CAT062 record into labelled octet groups; fail if the lengths do not add up."""
    b = bytes.fromhex(hexstr)
    if b[0] != 0x3E or int.from_bytes(b[1:3], "big") != len(b):
        sys.exit("site/build.py: the CAT062 payload's category or length octets are not what the page expects")
    parts = [("CAT", b[0:1]), ("LEN", b[1:3])]
    i, fspec = 3, []
    while True:
        fspec.append(b[i])
        i += 1
        if not b[i - 1] & 1:
            break
    parts.append(("FSPEC", b[3:i]))
    frn = 0
    for octet in fspec:
        for bit in range(7, 0, -1):
            item = UAP[frn] if frn < len(UAP) else None
            frn += 1
            if not octet >> bit & 1:
                continue
            if item in FIXED:
                n = FIXED[item]
            elif item == "080":
                n = 1
                while b[i + n - 1] & 1:
                    n += 1
            else:
                sys.exit(f"site/build.py: CAT062 item at FRN {frn} is not one the page can label")
            parts.append(("I062/" + item, b[i : i + n]))
            i += n
    if i != len(b):
        sys.exit("site/build.py: the CAT062 items do not add up to the record length")
    return [{"label": k, "hex": v.hex(" ")} for k, v in parts]


def measure():
    inputs = [
        p for p in glob.glob(os.path.join(ROOT, "src/open_entity_gateway/inputs/*.py")) if not p.endswith("__init__.py")
    ]
    with open(os.path.join(ROOT, "tests/vectors/cotify.jsonl")) as f:
        cot_vectors = sum(1 for line in f if line.strip())
    sdk = 0
    for path in glob.glob(os.path.join(ROOT, "requirements/*.txt")):
        with open(path) as f:
            sdk += sum(1 for line in f if re.match(r"\s*(anduril|picogrid)", line, re.I))
    return {
        "bridges": str(
            len(
                [
                    d
                    for d in os.listdir(os.path.join(ROOT, "vendors"))
                    if os.path.isfile(os.path.join(ROOT, "vendors", d, "pyproject.toml"))
                ]
            )
        ),
        "inputs": str(len(inputs)),
        "cot-vectors": str(cot_vectors),
        "sdk": str(sdk),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in ("where-it-fits.svg", "inside.svg"):
        shutil.copyfile(os.path.join(ROOT, "docs/img", name), os.path.join(OUT, name))
    with open(os.path.join(ROOT, RECORDS)) as f:
        record = next(json.loads(line) for line in f if '"message_type":"Position"' in line)
    with open(os.path.join(ROOT, MAPPING)) as f:
        mapping = f.read()
    outputs = {a: gateway_print(a) for a in ADAPTERS}
    hero = {
        "records": RECORDS,
        "mapping_file": MAPPING,
        "record": {
            "subsystem": record["subsystem"],
            "message_type": record["message_type"],
            "dictionary": record.get("dictionary"),
            "message": record["message"],
        },
        "mapping": mapping,
        "lattice": json.loads(outputs["lattice"]),
        "cot": outputs["cot"],
        "cat062": split_cat062(outputs["asterix-cat062"]),
    }
    with open(os.path.join(OUT, "hero.json"), "w") as f:
        json.dump(hero, f, indent=1)

    with open(os.path.join(SITE, "index.html")) as f:
        page = f.read()
    bad = 0
    shown = re.search(r'<pre class="out" id="out-static"[^>]*><code>(.*?)</code></pre>', page, re.S)
    if not shown or html.unescape(shown.group(1)).strip() != outputs["cot"]:
        print("site/index.html: the static CoT event is not what the gateway prints:\n" + outputs["cot"])
        bad += 1
    measured = measure()
    stated = dict(re.findall(r'data-measure="([a-z0-9-]+)">([^<]*)<', page))
    for key, value in measured.items():
        if stated.get(key) != value:
            print(f"site/index.html: {key} says {stated.get(key)}, the tree gives {value}")
            bad += 1
    for key in stated.keys() - measured.keys():
        print(f'site/index.html: data-measure="{key}" is not measured by site/build.py')
        bad += 1
    if bad:
        return 1
    print("site: numbers match the tree:", ", ".join(f"{k}={v}" for k, v in measured.items()))
    return subprocess.run([sys.executable, "-I", os.path.join(SITE, "fitcheck.py"), SITE, "index.html"]).returncode  # noqa: S603


if __name__ == "__main__":
    sys.exit(main())
