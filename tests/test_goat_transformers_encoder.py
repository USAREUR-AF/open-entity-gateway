"""`encoder: goat-transformers` against the native encoders.

Every example record and track is encoded both ways for CoT, ASTERIX CAT062
and SAPIENT (every DetectionReport field the generated bindings read), the
outputs are parsed and compared field by field, and every field
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


def flat_message(message: Any, path: str = "detection_report") -> dict[str, str]:
    """Every field of a protobuf message that is set or explicitly present."""
    fields: dict[str, str] = {}
    for descriptor, value in message.ListFields():
        here = f"{path}.{descriptor.name}"
        for index, item in enumerate(list(value) if descriptor.is_repeated else [value]):
            key = f"{here}[{index}]" if descriptor.is_repeated else here
            if descriptor.message_type is not None:
                fields.update(flat_message(item, key))
            elif descriptor.enum_type is not None:
                fields[key] = descriptor.enum_type.values_by_number[item].name
            else:
                fields[key] = str(item)
    return fields


def sapient_node(encoder: str) -> Any:
    from open_entity_gateway.adapters.sapient import SapientEdgeNode

    return SapientEdgeNode({"name": "s", "adapter": "sapient", "print": True, "encoder": encoder})


def same(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except ValueError:
        return False


def differences(sapient: bool = False) -> list[dict[str, str]]:
    rows = []
    formats: list[tuple[str, Any, Any]] = [
        ("cot", lambda enc: CursorOnTarget({"name": "c", "adapter": "cot", "print": True, "encoder": enc}), flat_xml),
        (
            "asterix-cat062",
            lambda enc: AsterixCat062(
                {"name": "a", "adapter": "asterix-cat062", "print": True, "sac": 25, "sic": 4, "encoder": enc}
            ),
            flat_cat062,
        ),
    ]
    if sapient:
        formats = [("sapient", sapient_node, flat_message)]
    for fmt, make, flat in formats:
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


def recorded(sapient: bool) -> list[dict[str, str]]:
    return [row for row in json.loads(BASELINE.read_text()) if (row["format"] == "sapient") == sapient]


def test_the_differences_are_the_recorded_baseline() -> None:
    rows = differences()
    if os.environ.get("OEG_COMPARE_UPDATE"):
        BASELINE.write_text(json.dumps(rows + recorded(sapient=True), indent=1) + "\n")
    assert rows == recorded(sapient=False)


def test_the_sapient_differences_are_the_recorded_baseline() -> None:
    pytest.importorskip("open_entity_gateway.adapters.sapient._generated.sapient_msg.proto_options_pb2")
    rows = differences(sapient=True)
    if os.environ.get("OEG_COMPARE_UPDATE"):
        BASELINE.write_text(json.dumps(recorded(sapient=False) + rows, indent=1) + "\n")
    assert rows == recorded(sapient=True)


def test_the_copied_engine_matches_its_sums() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sync-goat-transformers.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def sync_tool() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_goat_transformers", ROOT / "tools" / "sync-goat-transformers.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_module_a_copied_pack_uses_is_copied() -> None:
    from open_entity_gateway import library

    assert sync_tool().missing_modules(library.DATA) == []


def test_a_module_a_pack_uses_and_the_copy_lacks_is_named(tmp_path: Any) -> None:
    import shutil

    from open_entity_gateway import library

    data = tmp_path / "data"
    shutil.copytree(library.DATA, data)
    (data / "packs" / "cot-xml" / "pack.def").write_text(
        (data / "packs" / "cot-xml" / "pack.def")
        .read_text()
        .replace("uses    track", "uses    never-copied\nuses    track", 1)
    )
    (data / "model" / "cat062.def").unlink()
    assert sync_tool().missing_modules(data) == [
        "packs/asterix-cat062/pack.def: uses cat062, and model/cat062.def is not copied",
        "packs/cot-xml/pack.def: uses never-copied, and model/never-copied.def is not copied",
    ]


def test_the_models_copied_are_those_the_packs_use(tmp_path: Any) -> None:
    root = tmp_path / "gt"
    for name, text in {
        "packs/canonical-track-v2/pack.def": "format a\nuses track\n",
        "packs/cot-xml/pack.def": "format b\nuses cot\nuses track\n",
        "packs/asterix-cat062/pack.def": "format c\nuses cat062\n",
        "packs/sapient/pack.def": "format d\nuses sapient\n",
        "model/core.def": "module core\n",
        "model/track.def": "module track\n",
        "model/cot.def": "module cot\n",
        "model/cat062.def": "module cat062\nuses track\n",
        "model/sapient.def": "module sapient\nuses track\n",
        "model/radar.def": "module radar\n",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    assert sync_tool().models(root) == ["cat062", "core", "cot", "sapient", "track"]


def test_an_unknown_encoder_is_refused() -> None:
    from open_entity_gateway.adapter import ConfigError

    with pytest.raises(ConfigError, match="encoder"):
        CursorOnTarget({"name": "c", "adapter": "cot", "print": True, "encoder": "other"})


@pytest.mark.parametrize(
    ("adapter", "key", "value"),
    [("cot", "time_form", "z")],
)
def test_a_key_only_the_native_encoder_writes_is_refused(adapter: str, key: str, value: Any) -> None:
    from open_entity_gateway.adapter import ConfigError

    cls = CursorOnTarget if adapter == "cot" else AsterixCat062
    with pytest.raises(ConfigError, match=f"{key} is written only by the native encoder"):
        cls({"name": "x", "adapter": adapter, "print": True, "encoder": "goat-transformers", key: value})


def test_a_pack_that_does_not_load_stops_the_destination_at_start(tmp_path: Any, monkeypatch: Any) -> None:
    import shutil

    from open_entity_gateway import library
    from open_entity_gateway.adapter import ConfigError

    data = tmp_path / "data"
    shutil.copytree(library.DATA, data)
    (data / "packs" / "cot-xml" / "pack.def").write_text("format cot-xml\nnot-a-directive\n")
    monkeypatch.setattr(library, "DATA", data)
    monkeypatch.setattr(library, "_engine", None)
    with pytest.raises(ConfigError, match="packs did not load"):
        CursorOnTarget({"name": "c", "adapter": "cot", "print": True, "encoder": "goat-transformers"})


def test_a_sapient_detection_from_the_library_keeps_the_gateway_identity(buoy_position: dict[str, Any]) -> None:
    pytest.importorskip("open_entity_gateway.adapters.sapient._generated.sapient_msg.proto_options_pb2")
    from open_entity_gateway.adapters.sapient import object_ulid

    node = sapient_node("goat-transformers")
    (m,) = [m for m in prepare_mappings([MAPPINGS], [node]) if m.matches(buoy_position)]
    detection = node.encode(m.track(buoy_position), buoy_position, node.block_for(m))
    assert detection.object_id == object_ulid(node.node_id, "7")
    assert (detection.id, detection.classification[0].type) == ("Buoy/7", "Buoy")
    assert not detection.HasField("report_id")  # the session sets it when sending
