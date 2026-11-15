#!/usr/bin/env python3
"""Fetch the MITRE Cursor-on-Target public-release schemas the tests check
every event against.

    python tools/fetch-cot-xsd.py [DIRECTORY]      # default: .cache/cot-xsd

The schemas are MITRE's ("Approved for Public Release; Distribution
Unlimited", stated in each file). They are read from the copy in
TAK-Product-Center/atak-civ, takcot/mitre/, at a pinned commit, checked
against their SHA-256 below, and kept out of this repository.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "TAK-Product-Center/atak-civ"
COMMIT = "9f6893dd657feacc35ec5de03dad721c2e44170e"
# Saved name: (path in the repository, SHA-256)
FILES = {
    "event.xsd": (
        "takcot/mitre/CoT Base-Event Schema  (PUBLIC RELEASE).xsd",
        "4ab1ce0f7e5d3f5396f0b3e19b993e68847dabf5f4e3f37af0b7cb0235d31983",
    ),
    "contact.xsd": (
        "takcot/mitre/CoT Contact Schema (PUBLIC RELEASE).xsd",
        "ada3f88e7168b4953b142c115390136213df3cde0794103d013c8cb7e22420f4",
    ),
    "track.xsd": (
        "takcot/mitre/CoT Track Schema  (PUBLIC RELEASE).xsd",
        "495c50036adca7416ce71ad87e1cd5b2aefe8648188eb402dac695651ba5773d",
    ),
}
# The three schemas share no namespace; this one includes them so one
# validator checks the event and the detail elements it knows.
ALL = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" elementFormDefault="qualified">
  <xs:include schemaLocation="event.xsd"/>
  <xs:include schemaLocation="contact.xsd"/>
  <xs:include schemaLocation="track.xsd"/>
</xs:schema>
"""


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / ".cache" / "cot-xsd")
    target.mkdir(parents=True, exist_ok=True)
    for saved, (path, expected) in FILES.items():
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{urllib.parse.quote(path)}"
        with urllib.request.urlopen(url, timeout=30) as answer:
            body = answer.read()
        digest = hashlib.sha256(body).hexdigest()
        if digest != expected:
            sys.exit(f"{path}: SHA-256 {digest}, expected {expected}; not using it")
        (target / saved).write_bytes(body)
    (target / "all.xsd").write_text(ALL, encoding="utf-8")
    print(f"MITRE CoT schemas in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
