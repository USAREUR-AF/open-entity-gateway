"""The Picogrid ECN adapter against the offline mock that ships in Picogrid's
ECN SDK (`picogrid_ecn_client.testing.MockECN`).

The gateway publishes the example tracks through the SDK to the mock's MQTT
broker; a second SDK client, watching that integration, receives and decodes
them. Skipped unless the SDK is installed (CI never installs it).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ecn = pytest.importorskip("picogrid_ecn_client")
from open_entity_gateway_picogrid_ecn import PicogridECN, entity_uuid  # noqa: E402
from picogrid_ecn_client.testing import MockECN  # noqa: E402

from open_entity_gateway.gateway import Destination, Gateway, prepare_mappings  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
INTEGRATION = "open-entity-gateway-test"


def records(name: str) -> list[dict[str, Any]]:
    text = (ROOT / "examples" / "records" / name).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class Loop:
    """An asyncio loop on its own thread, for the mock and the watcher."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coroutine: Any, timeout: float = 20) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def mock() -> Any:
    loop = Loop()
    instance = MockECN(host="127.0.0.1", mqtt_port=0)
    loop.run(instance.start())
    yield loop, instance
    loop.run(instance.close())
    loop.stop()


def test_tracks_reach_a_watcher_through_the_mock(mock: Any) -> None:
    loop, instance = mock
    watched: list[Any] = []

    async def watch(ready: asyncio.Event, count: int) -> None:
        async with ecn.ECNClient(instance.client_config("watcher")) as client:
            stream = await client.entities.watch(
                integrations={INTEGRATION},
                categories={ecn.EntityCategory.DEVICE, ecn.EntityCategory.TRACK},
                delivery=ecn.DeliveryPolicy.FIFO,
            )
            ready.set()
            try:
                async for event in stream:
                    watched.append(event)
                    if len(watched) >= count:
                        return
            finally:
                await stream.aclose()

    async def start_watcher() -> asyncio.Future[None]:
        ready = asyncio.Event()
        task = asyncio.ensure_future(watch(ready, 2))
        await asyncio.wait_for(ready.wait(), 10)
        return task

    watcher = loop.run(start_watcher())

    adapter = PicogridECN({"name": "ecn", "adapter": "picogrid-ecn"}, config=instance.client_config(INTEGRATION))
    mappings = prepare_mappings([ROOT / "mappings"], [adapter])
    gateway = Gateway(mappings, [Destination(adapter, None, 10)])
    gateway.start()
    weather = next(r for r in records("weather-station.records.jsonl") if r["message_type"] == "Position")
    buoy = next(r for r in records("harbour-buoy.records.jsonl") if r["message_type"] == "Position")
    assert gateway.handle(weather) == "sent"
    assert gateway.handle(buoy) == "sent"
    deadline = time.monotonic() + 10
    while gateway.destinations[0].counts.sent < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    loop.run(asyncio.wait_for(asyncio.shield(watcher), 15))
    gateway.close()

    assert gateway.destinations[0].counts.sent == 2
    assert [r.entity_id for r in adapter.receipts] == [entity_uuid(INTEGRATION, "KX7"), entity_uuid(INTEGRATION, "7")]
    print("receipts:", [(r.kind, r.qos, r.delivery_phase) for r in adapter.receipts])

    by_id = {event.entity.id: event for event in watched}
    station = by_id[entity_uuid(INTEGRATION, "KX7")].entity
    assert (station.category, station.type, station.name) == (ecn.EntityCategory.DEVICE, "weather-station", "KX7")
    assert station.affiliation == ecn.Affiliation.FRIEND
    assert station.position is not None
    assert (station.position.latitude, station.position.longitude, station.position.altitude) == (
        64.8378,
        -147.7164,
        136.5,
    )
    assert station.recorded_at.timestamp() == 1_750_000_003

    drifting = by_id[entity_uuid(INTEGRATION, "7")].entity
    assert (drifting.category, drifting.type, drifting.name) == (ecn.EntityCategory.TRACK, "a-f-S", "Buoy/7")
    assert drifting.position is not None and drifting.position.velocity is not None
    assert drifting.position.velocity.north == pytest.approx(-0.2474873734)
    assert drifting.position.velocity.east == pytest.approx(0.2474873734)
    print("watched:", [e.entity.model_dump_json(exclude_none=True) for e in watched])

    state = instance.entity_state
    print("mock entity state keys:", sorted(state))
    assert len(state) >= 2


def test_a_mapping_block_outside_the_sdk_categories_is_refused() -> None:
    from open_entity_gateway.mapping import Mapping, MappingError

    m = Mapping(
        {
            "match": {"subsystem": "A", "message_type": "B"},
            "uid": ["id"],
            "type": {"value": "a-u-G"},
            "position": {"geodetic": ["lat", "lon"]},
            "picogrid_ecn": {"category": "ASSET"},
        },
        "t",
        blocks=["picogrid_ecn"],
    )
    with pytest.raises(MappingError, match="category"):
        PicogridECN.parse_block(m.blocks["picogrid_ecn"], m)
