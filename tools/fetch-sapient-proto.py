#!/usr/bin/env python3
"""Fetch the SAPIENT (BSI Flex 335 v2.0) proto files at a pinned commit and
generate the Python bindings the SAPIENT adapter imports.

    python tools/fetch-sapient-proto.py

The proto files are Dstl's, Crown copyright, under the Apache License 2.0
(https://github.com/dstl/SAPIENT-Proto-Files, LICENCE.txt). They are not kept
in this repository. Each file is fetched from the pinned commit and checked
against its SHA-256 below; a file that differs stops the tool.

One change is made before generating: the `import "sapient_msg/...` lines
are rewritten to the path the files are placed at, so the generated modules
live inside this package and do not take the top-level name `sapient_msg`.
Messages, fields and package names are not changed.

Needs grpcio-tools (requirements/tools.txt). Writes
src/open_entity_gateway/adapters/sapient/_generated/, which git ignores.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

REPOSITORY = "dstl/SAPIENT-Proto-Files"
COMMIT = "244a741c13bcee628f84944aef91e4a66854bd7d"  # main, 2024-06-04
FILES = {
    "LICENCE.txt": "d7c96112b11c69ef0332e91dbe79115672535b5f117c75432f8b6604f8c594f6",
    "proto_options.proto": "9e9870d6b8f497969ee6283d4664c47a68ce036c83b3355422c312fc721f2268",
    "bsi_flex_335_v2_0/alert_ack.proto": "2feaec808296dc90a1f64d333bca6bc750edb48da1f198f65d9ac5711c5307c1",
    "bsi_flex_335_v2_0/alert.proto": "d9c4392c9e63f68bba776755057527786f889b4022c97252cf14a26eacd7d3ff",
    "bsi_flex_335_v2_0/associated_detection.proto": "35615503d11d5e8a42073531ff8bfd8b8c4f2eb92af125f53bc9a8106fdf3244",
    "bsi_flex_335_v2_0/associated_file.proto": "128bb94aed8c8b47594c89d672f8c26cf8e658567428e0b15e26e1892eeebbf8",
    "bsi_flex_335_v2_0/detection_report.proto": "d597b0bac12080f004ec43c481c817a3ecf0df820cc4908451eb5a34573b04b6",
    "bsi_flex_335_v2_0/error.proto": "977dbe0d6434712d5ede2c5c65d7c3b80b008f94a8330d2bc1ede56b8cc2db23",
    "bsi_flex_335_v2_0/follow.proto": "cdcf2ec4265ad63349c2ee96e704f7113ff398f958520f3b1c75153b39873dbc",
    "bsi_flex_335_v2_0/location.proto": "3a7d3c000349176cf5fdbe5b5ab0dd8e1cef061815b32c43621478e39daadcca",
    "bsi_flex_335_v2_0/range_bearing.proto": "9230fe7943f2b8b28daee40ec2f4357467f256b26496d44bfc3cab8e8f646ab3",
    "bsi_flex_335_v2_0/registration_ack.proto": "fbb4b4e7f09e7bf68369e4b9664cb5dd59245991310f2637608692a17fcf1a35",
    "bsi_flex_335_v2_0/registration.proto": "84ad97823aa1ed8f3dbaf18adfea3d4a01e68b26e62619baa6bea5f52c699c5a",
    "bsi_flex_335_v2_0/sapient_message.proto": "e11ea1d3fe3fbccb13aafb53bca2187fc2e2e66418578a91cfdb42f89a055426",
    "bsi_flex_335_v2_0/status_report.proto": "a2675a6d0c0026912e2cb750fd93de0bdaa2c59d32cd424cacb694fc7c83db23",
    "bsi_flex_335_v2_0/task_ack.proto": "78b94c1c75e227f50b5bf4afde729b8bc2666cac3f675b13cb9b6084d2c3e3b7",
    "bsi_flex_335_v2_0/task.proto": "956076d43ac06f79df711fa0c5a695f1c10102389eb4c25726936f30df8161a2",
    "bsi_flex_335_v2_0/velocity.proto": "ac74090ecf1d7bcb68376ca5780e7b096fbdd524c8055837ace26502f4f14dad",
}

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "open_entity_gateway/adapters/sapient/_generated"
OUT = ROOT / "src" / PACKAGE


def fetch(name: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{name}"
    with urllib.request.urlopen(url, timeout=30) as answer:
        body: bytes = answer.read()
    digest = hashlib.sha256(body).hexdigest()
    if digest != FILES[name]:
        sys.exit(f"{name}: SHA-256 {digest}, expected {FILES[name]}; not using it")
    return body


def main() -> int:
    try:
        from grpc_tools import protoc
    except ImportError:
        sys.exit("needs grpcio-tools: pip install -r requirements/tools.txt")
    import importlib.resources

    with tempfile.TemporaryDirectory() as work:
        source = Path(work)
        protos = []
        for name, _ in FILES.items():
            body = fetch(name)
            if name.endswith(".proto"):
                body = body.replace(b'import "sapient_msg/', f'import "{PACKAGE}/sapient_msg/'.encode())
                target = source / PACKAGE / "sapient_msg" / name
                protos.append(str(target.relative_to(source)))
            else:
                target = source / PACKAGE / "sapient_msg" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        include = str(importlib.resources.files("grpc_tools") / "_proto")
        status = protoc.main(["protoc", f"-I{source}", f"-I{include}", f"--python_out={ROOT / 'src'}", *protos])
        if status != 0:
            sys.exit(f"protoc failed with status {status}")
        for directory in [OUT, *[p for p in OUT.rglob("*") if p.is_dir()]]:
            (directory / "__init__.py").touch()
        shutil.copy(source / PACKAGE / "sapient_msg" / "LICENCE.txt", OUT / "LICENCE.txt")
        (OUT / "SOURCE").write_text(
            f"Generated from https://github.com/{REPOSITORY} at commit {COMMIT}\n"
            "by tools/fetch-sapient-proto.py. Crown copyright, Apache License 2.0 (LICENCE.txt).\n",
            encoding="utf-8",
        )
    print(f"generated {len(protos)} modules in {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
