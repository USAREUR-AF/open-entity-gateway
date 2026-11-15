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
