"""`encoder: goat-transformers` against the native encoders.

Every example record and track is encoded both ways for CoT and ASTERIX
CAT062, the outputs are parsed and compared field by field, and every field
that differs is a row of tests/vectors/goat-transformers-baseline.json. The
test fails when a row appears, disappears or changes. After a deliberate
change, run it with OEG_COMPARE_UPDATE=1 and give each new row its cause in
docs/goat-transformers-encoder.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Any

import asterix4py
import pytest

from open_entity_gateway.adapters.asterix import AsterixCat062
from open_entity_gateway.adapters.cot import CursorOnTarget
from open_entity_gateway.canonical_track import is_canonical_track, track_record
from open_entity_gateway.gateway import prepare_mappings

from .conftest import MAPPINGS, ROOT

BASELINE = ROOT / "tests" / "vectors" / "goat-transformers-baseline.json"


def inputs() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted((ROOT / "examples").glob("*/*.jsonl")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip():
                value = json.loads(line)
                record = track_record(value) if is_canonical_track(value) else value
                out.append((f"{path.relative_to(ROOT)}:{number}", record))
    return out


def flat_xml(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}

    def walk(element: ET.Element, path: str) -> None:
        here = f"{path}/{element.tag}"
        fields[here] = (element.text or "").strip()
        for name, value in element.attrib.items():
            fields[f"{here}/@{name}"] = value
        for child in element:
            walk(child, here)

    walk(ET.fromstring(payload), "")
    return fields


def flat_cat062(payload: bytes) -> dict[str, str]:
    item = asterix4py.AsterixParser(payload).get_result()[1]
    fields = {}
    for key, value in item.items():
        if isinstance(value, dict):
            fields.update({f"I062/{key}/{sub}": str(v) for sub, v in value.items()})
        else:
            fields[f"I062/{key}"] = str(value)
    return fields


def same(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except ValueError:
        return False


def differences() -> list[dict[str, str]]:
    rows = []
    for fmt, make, flat in (
        ("cot", lambda enc: CursorOnTarget({"name": "c", "adapter": "cot", "print": True, "encoder": enc}), flat_xml),
        (
            "asterix-cat062",
            lambda enc: AsterixCat062(
                {"name": "a", "adapter": "asterix-cat062", "print": True, "sac": 25, "sic": 4, "encoder": enc}
            ),
            flat_cat062,
        ),
    ):
        for name, record in inputs():
            outputs = []
            for encoder in ("native", "goat-transformers"):
                adapter = make(encoder)
                mappings = prepare_mappings([MAPPINGS], [adapter])
                chosen = [m for m in mappings if m.matches(record)]
                if not chosen:
                    break
                track = chosen[0].track(record, received_ns=0)
                outputs.append(flat(adapter.encode(track, record, adapter.block_for(chosen[0]))))
            if len(outputs) != 2:
                continue
            native, library = outputs
            for field in sorted(set(native) | set(library)):
                if not same(native.get(field), library.get(field)):
                    rows.append(
                        {
                            "format": fmt,
                            "input": name,
                            "field": field,
                            "native": native.get(field, "(absent)"),
                            "library": library.get(field, "(absent)"),
                        }
                    )
    return rows


def test_the_differences_are_the_recorded_baseline() -> None:
    rows = differences()
    if os.environ.get("OEG_COMPARE_UPDATE"):
        BASELINE.write_text(json.dumps(rows, indent=1) + "\n")
    assert rows == json.loads(BASELINE.read_text())


def test_the_copied_engine_matches_its_sums() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sync-goat-transformers.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_an_unknown_encoder_is_refused() -> None:
    from open_entity_gateway.adapter import ConfigError

    with pytest.raises(ConfigError, match="encoder"):
        CursorOnTarget({"name": "c", "adapter": "cot", "print": True, "encoder": "other"})
