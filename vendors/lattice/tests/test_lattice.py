"""The Lattice adapter. The entity tests need no SDK. The publishing tests run
only when the Lattice SDK is installed; they give the SDK an HTTP transport
that keeps each request and answers locally, so nothing leaves the machine.
Not run against a Lattice environment."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from open_entity_gateway_lattice import Lattice

from open_entity_gateway.adapter import ConfigError, DeliveryError
from open_entity_gateway.gateway import Destination, Gateway, prepare_mappings

ROOT = Path(__file__).resolve().parents[3]
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
ENVIRONMENT = {
    "LATTICE_ENDPOINT": "lattice.example.org",
    "ENVIRONMENT_TOKEN": "environment-token-for-tests",
    "SANDBOXES_TOKEN": "sandboxes-token-for-tests",
}

# What the weather-station Position record becomes with mappings/weather-station.position.yml,
# published at NOW. sourceUpdateTime is the capture time; expiryTime is NOW plus stale_minutes.
WEATHER_STATION_ENTITY = {
    "entityId": "weather-station/WX/KX7",
    "isLive": True,
    "expiryTime": "2026-01-01T00:10:00Z",
    "aliases": {"name": "KX7"},
    "location": {"position": {"latitudeDegrees": 64.8378, "longitudeDegrees": -147.7164, "altitudeHaeMeters": 136.5}},
    "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_LAND"},
    "ontology": {"template": "TEMPLATE_ASSET", "platformType": "Weather station"},
    "provenance": {
        "integrationName": "open-entity-gateway",
        "dataType": "weather-station/WX/Position",
        "sourceId": "KX7",
        "sourceUpdateTime": "2025-06-15T15:06:43Z",
    },
    "indicators": {"simulated": True},
}
HARBOUR_BUOY_ENTITY = {
    "entityId": "7",
    "isLive": True,
    "expiryTime": "2026-01-01T00:03:00Z",
    "aliases": {"name": "Buoy/7"},
    "location": {
        "position": {"latitudeDegrees": 44.5012345, "longitudeDegrees": -30.254321, "altitudeHaeMeters": -1.5}
    },
    "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_SURFACE"},
    "ontology": {"template": "TEMPLATE_TRACK"},
    "provenance": {
        "integrationName": "open-entity-gateway",
        "dataType": "harbour-buoy/BUOY/Position",
        "sourceId": "7",
        "sourceUpdateTime": "2025-06-15T15:06:40Z",
    },
}


def records(name: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (ROOT / "examples" / "records" / name).read_text().splitlines() if x.strip()]


def position(name: str) -> dict[str, Any]:
    return next(r for r in records(name) if r["message_type"] == "Position")


def entity_for(name: str) -> dict[str, Any]:
    adapter = Lattice({"name": "l", "adapter": "lattice", "print": True}, clock=lambda: NOW)
    mappings = prepare_mappings([ROOT / "mappings"], [adapter])
    record = position(name)
    (m,) = [m for m in mappings if m.matches(record)]
    return adapter.encode(m.track(record), record, m.parsed["lattice"])


def test_the_weather_station_entity() -> None:
    assert entity_for("weather-station.records.jsonl") == WEATHER_STATION_ENTITY


def test_the_harbour_buoy_entity_with_the_defaults() -> None:
    assert entity_for("harbour-buoy.records.jsonl") == HARBOUR_BUOY_ENTITY


def test_the_sdk_is_not_needed_to_start_without_credentials() -> None:
    adapter = Lattice({"name": "l", "adapter": "lattice"}, environ={})
    with pytest.raises(ConfigError, match="LATTICE_ENDPOINT and ENVIRONMENT_TOKEN"):
        adapter.start()
    adapter = Lattice(
        {"name": "l", "adapter": "lattice"},
        environ={"LATTICE_ENDPOINT": "https://lattice.example.org", "ENVIRONMENT_TOKEN": "t"},
    )
    with pytest.raises(ConfigError, match="host name only"):
        adapter.start()


# -- with the SDK ------------------------------------------------------------------


class Service:
    """Stands in for the service: keeps each request, answers as told."""

    def __init__(self, status: int = 200):
        self.requests: list[Any] = []
        self.status = status

    def __call__(self, request: Any) -> Any:
        import httpx

        self.requests.append(request)
        body = json.loads(request.content) if self.status == 200 else {"message": "refused"}
        return httpx.Response(self.status, json=body)


def sdk_gateway(service: Service, environ: dict[str, str] = ENVIRONMENT) -> Gateway:
    pytest.importorskip("anduril", reason="the Lattice SDK is installed separately, under its vendor's licence")
    import httpx

    adapter = Lattice(
        {"name": "l", "adapter": "lattice"},
        environ=environ,
        httpx_client=httpx.Client(transport=httpx.MockTransport(service)),
        clock=lambda: NOW,
    )
    mappings = prepare_mappings([ROOT / "mappings"], [adapter])
    return Gateway(mappings, [Destination(adapter, None, 10)])


def run(gateway: Gateway, items: list[dict[str, Any]]) -> list[str]:
    gateway.start()
    outcomes = [gateway.handle(r) for r in items]
    gateway.close()
    return outcomes


def test_the_sdk_is_asked_to_send_the_expected_request() -> None:
    service = Service()
    outcomes = run(sdk_gateway(service), records("weather-station.records.jsonl"))
    assert outcomes == ["skipped", "skipped", "skipped", "sent", "skipped"]
    (request,) = service.requests
    assert request.method == "PUT"
    assert str(request.url) == "https://lattice.example.org/api/v1/entities"
    assert json.loads(request.content) == WEATHER_STATION_ENTITY
    assert request.headers["authorization"] == "Bearer environment-token-for-tests"
    assert request.headers["anduril-sandbox-authorization"] == "Bearer sandboxes-token-for-tests"


def test_the_harbour_buoy_request() -> None:
    service = Service()
    run(sdk_gateway(service), [position("harbour-buoy.records.jsonl")])
    assert json.loads(service.requests[0].content) == HARBOUR_BUOY_ENTITY


def test_no_sandbox_header_without_a_sandboxes_token() -> None:
    service = Service()
    environ = {k: v for k, v in ENVIRONMENT.items() if k != "SANDBOXES_TOKEN"}
    run(sdk_gateway(service, environ), [position("weather-station.records.jsonl")])
    assert "anduril-sandbox-authorization" not in service.requests[0].headers


def test_a_refusal_is_counted_and_carries_no_token(caplog: pytest.LogCaptureFixture) -> None:
    service = Service(status=400)
    gateway = sdk_gateway(service)
    run(gateway, [position("weather-station.records.jsonl")])
    assert gateway.destinations[0].counts.failed == 1
    assert "400" in caplog.text and "token-for-tests" not in caplog.text


def test_a_record_its_mapping_cannot_fill_is_not_sent() -> None:
    service = Service()
    record = position("weather-station.records.jsonl")
    record = {**record, "message": {**record["message"], "longitude": None}}
    assert run(sdk_gateway(service), [record]) == ["refused"]
    assert service.requests == []


def test_delivery_error_names_no_secret() -> None:
    assert "token" not in str(DeliveryError("Lattice answered 401"))
