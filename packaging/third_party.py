#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Copy every installed distribution's licence files and write THIRD_PARTY.txt.

    python3 -I packaging/third_party.py <lib> <licenses>

<lib> is a pip --target directory. For each *.dist-info: its name, version, licence (the
License-Expression, else the License field, else its classifiers) and project URLs go in
THIRD_PARTY.txt, and its licence files (PEP 639 licenses/, else LICENSE*, LICENCE*,
COPYING*, NOTICE* in the dist-info) are copied to <licenses>/<name>-<version>/. A
distribution with no licence file is a failure: the archive would ship it without its notice.
"""

import email.parser
import pathlib
import shutil
import sys

NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def main(lib: pathlib.Path, out: pathlib.Path) -> int:
    rows, missing = [], []
    for info in sorted(lib.glob("*.dist-info")):
        meta = email.parser.Parser().parsestr((info / "METADATA").read_text(encoding="utf-8"), headersonly=True)
        name, version = meta["Name"], meta["Version"]
        licence = meta.get("License-Expression") or (meta.get("License") or "").splitlines()[0:1]
        if isinstance(licence, list):
            licence = licence[0] if licence else ""
        if not licence:
            licence = "; ".join(
                c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")
            )
        files = [
            p
            for p in info.rglob("*")
            if p.is_file()
            and ((info / "licenses") in p.parents or (info / "sboms") in p.parents or p.name.upper().startswith(NAMES))
        ]
        if not files and name.startswith("open-entity-gateway") and (out / "LICENSE").is_file():
            # This repository's own packages: the archive's licenses/LICENSE and NOTICE.
            rows.append(
                f"{name} {version}\n  licence: {licence or 'Apache-2.0'}\n"
                "  files: licenses/LICENSE, licenses/NOTICE\n"
                "  source: this release's tag of the source repository\n"
            )
            continue
        if not files:
            missing.append(f"{name} {version}")
            continue
        dest = out / f"{name}-{version}"
        for f in files:
            target = dest / f.relative_to(info / "licenses" if (info / "licenses") in f.parents else info)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
        urls = [u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []]
        urls += [meta["Home-page"]] if meta.get("Home-page") else []
        rows.append(
            f"{name} {version}\n  licence: {licence or 'see its licence files'}\n"
            f"  files: licenses/{dest.name}/\n"
            f"  source: {', '.join(urls) or 'https://pypi.org/project/' + name + '/'}\n"
        )
    sapient = lib / "open_entity_gateway" / "adapters" / "sapient" / "_generated" / "LICENCE.txt"
    if sapient.is_file():
        (out / "sapient-proto-files").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sapient, out / "sapient-proto-files" / "LICENCE.txt")
        rows.append(
            "SAPIENT proto files (BSI Flex 335 v2.0), generated into open_entity_gateway\n"
            "  licence: Apache-2.0, Crown copyright\n  files: licenses/sapient-proto-files/\n"
            "  source: https://github.com/dstl/SAPIENT-Proto-Files\n"
        )
    if missing:
        print("third_party: no licence file in: " + ", ".join(missing), file=sys.stderr)
        return 1
    head = (
        "Every distribution this archive carries, with its licence and where its source is.\n"
        "A distribution under the Mozilla Public License 2.0 is shipped unmodified; its source\n"
        "is at the source location given for it.\n\n"
    )
    (out / "THIRD_PARTY.txt").write_text(head + "\n".join(rows), encoding="utf-8")
    print(f"third_party: {len(rows)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))
