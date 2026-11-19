from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "examples" / "records"
MAPPINGS = ROOT / "mappings"
XSD = ROOT / ".cache" / "cot-xsd" / "all.xsd"


def lines(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (RECORDS / name).read_text().splitlines() if line.strip()]


@pytest.fixture
def ws_records() -> list[dict[str, Any]]:
    return lines("weather-station.records.jsonl")


@pytest.fixture
def ws_position(ws_records: list[dict[str, Any]]) -> dict[str, Any]:
    return next(r for r in ws_records if r["message_type"] == "Position")


@pytest.fixture
def buoy_position() -> dict[str, Any]:
    return next(r for r in lines("harbour-buoy.records.jsonl") if r["message_type"] == "Position")


@pytest.fixture(scope="session")
def cot_schema() -> Any:
    """The MITRE public-release CoT schemas (tools/fetch-cot-xsd.py)."""
    if not XSD.exists():
        pytest.fail(f"{XSD} is missing: run python tools/fetch-cot-xsd.py")
    import xmlschema

    return xmlschema.XMLSchema(str(XSD))


@pytest.fixture(params=["native", "goat-transformers"])
def each_encoder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Runs a test once per encoder: every CoT, CAT062 and SAPIENT destination
    the test builds without an `encoder` key gets this one. A test marked
    `goat_differs(reason, params=...)` is expected to fail on goat-transformers
    for the recorded difference its reason names
    (docs/goat-transformers-encoder.md); strict, so it fails when the
    difference goes away. `params` limits the mark to the parametrisations
    whose values it lists."""
    from open_entity_gateway.adapters.asterix import AsterixCat062
    from open_entity_gateway.adapters.cot import CursorOnTarget
    from open_entity_gateway.adapters.sapient import SapientEdgeNode

    encoder: str = request.param
    for cls in (CursorOnTarget, AsterixCat062, SapientEdgeNode):
        original = cls.__init__

        def init(self: Any, destination: dict[str, Any], original: Any = original) -> None:
            original(self, {"encoder": encoder, **destination})

        monkeypatch.setattr(cls, "__init__", init)
    marker = request.node.get_closest_marker("goat_differs")
    if encoder == "goat-transformers" and marker is not None:
        callspec = getattr(request.node, "callspec", None)
        given = callspec.params if callspec is not None else {}
        if all(given.get(key) == value for key, value in marker.kwargs.get("params", {}).items()):
            request.applymarker(pytest.mark.xfail(reason=marker.args[0], strict=True))
    return encoder
