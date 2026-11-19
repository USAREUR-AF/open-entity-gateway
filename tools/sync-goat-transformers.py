"""Copy the goat-transformers Python engine and the files the gateway uses
into src/open_entity_gateway/_goat_transformers, or check the copy.

goat-transformers is not published on a package index, and the gateway
installs only hash-locked packages, so the engine (standard library only)
and its four model modules and four packs are copied at one commit, as
open-billie copies its packs. COMMIT names the commit and SHA256SUMS lists
every copied file.

    python tools/sync-goat-transformers.py <goat-transformers checkout>          # copy
    python tools/sync-goat-transformers.py --check                               # hashes only
    python tools/sync-goat-transformers.py --check <goat-transformers checkout>  # and equal to the checkout
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "src" / "open_entity_gateway" / "_goat_transformers"
ENGINE = [
    "__init__.py", "engine.py", "entity.py", "errors.py", "modeldef.py", "packdef.py", "seqdef.py",
    "transforms.py", "wire_bits.py", "wire_geojson.py", "wire_json.py", "wire_protobuf.py", "wire_seq.py",
    "wire_text.py", "wire_xml.py",
]  # fmt: skip
MODELS = ["core", "track", "cat062", "sapient"]
PACKS = ["canonical-track-v2", "cot-xml", "asterix-cat062", "sapient"]


def files(source: Path) -> dict[str, Path]:
    """Copied path (relative to DEST) -> source path."""
    out = {f"engine/{name}": source / "engines/python/src/goat_transformers" / name for name in ENGINE}
    out.update({f"data/model/{m}.def": source / "model" / f"{m}.def" for m in MODELS})
    out.update({f"data/packs/{p}/pack.def": source / "packs" / p / "pack.def" for p in PACKS})
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listed() -> dict[str, str]:
    lines = (DEST / "SHA256SUMS").read_text().splitlines()
    return {name: sha for sha, name in (line.split("  ", 1) for line in lines if line.strip())}


def check(source: Path | None) -> int:
    bad = []
    sums = listed()
    present = {str(p.relative_to(DEST)) for p in DEST.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    for name in sorted(present - set(sums) - {"SHA256SUMS", "COMMIT", "README.md", "__init__.py"}):
        bad.append(f"{name}: copied but not listed in SHA256SUMS")
    for name, sha in sums.items():
        path = DEST / name
        if not path.is_file():
            bad.append(f"{name}: listed but missing")
        elif digest(path) != sha:
            bad.append(f"{name}: differs from SHA256SUMS")
    if source is not None:
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        if commit != (DEST / "COMMIT").read_text().strip():
            bad.append(f"the checkout is at {commit}, COMMIT names {(DEST / 'COMMIT').read_text().strip()}")
        for name, src in files(source).items():
            if (DEST / name).is_file() and digest(src) != digest(DEST / name):
                bad.append(f"{name}: differs from the checkout")
    for line in bad:
        print(f"sync-goat-transformers: {line}", file=sys.stderr)
    print("sync-goat-transformers: " + ("FAILED" if bad else f"ok, {len(sums)} files"))
    return 1 if bad else 0


def copy(source: Path) -> int:
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    for sub in ("engine", "data"):
        shutil.rmtree(DEST / sub, ignore_errors=True)
    sums = []
    for name, src in sorted(files(source).items()):
        target = DEST / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        sums.append(f"{digest(target)}  {name}")
    (DEST / "COMMIT").write_text(commit + "\n")
    (DEST / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    print(f"sync-goat-transformers: copied {len(sums)} files from {commit}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("source", nargs="?", type=Path)
    args = parser.parse_args()
    if args.check:
        return check(args.source)
    if args.source is None:
        parser.error("give the goat-transformers checkout to copy from")
    return copy(args.source)


if __name__ == "__main__":
    sys.exit(main())
