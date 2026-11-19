"""A live check, not a pytest test: publish a track to an ECN, see it in OrionX.

Two roles, run on two machines, so no credential is passed to the ECN's host.

  publish  runs where the ECN's broker is reachable (the SDK reads its ECN_*
           variables or a profile; see docs/adapters/picogrid-ecn.md). A second
           SDK client watches the integration. The harbour-buoy and weather-station
           example tracks are published with their time moved to now every few
           seconds, then the
           example entities are published INACTIVE as cleanup (the SDK has no
           delete). It exits non-zero unless every send succeeded, every QoS 1
           publish was acknowledged by the broker, and the watcher decoded
           both entities.

  verify   runs anywhere that reaches OrionX. It polls OrionX's /api/state
           until a track from the test integration appears and prints it. The
           address and login come from ORIONX_URL (https only), ORIONX_USER and
           ORIONX_PASSWORD. ORIONX_CA names a file holding the certificate to
           trust. --insecure skips certificate verification and is the only way
           to reach an OrionX whose certificate is not in ORIONX_CA.

    python vendors/picogrid-ecn/live/live_orionx.py publish --seconds 120
    ORIONX_CA=orionx-ca.pem python vendors/picogrid-ecn/live/live_orionx.py verify --seconds 150

It writes test entities to the ECN.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INTEGRATION = "open-entity-gateway-test"


def publish(seconds: float, interval: float) -> int:
    import picogrid_ecn_client as ecn
    import yaml
    from open_entity_gateway_picogrid_ecn import PicogridECN, entity_uuid

    from open_entity_gateway.gateway import Destination, Gateway, prepare_mappings
    from open_entity_gateway.inputs.replay import read_records, shift_to

    def first(name: str) -> dict:
        return next(
            r for r in read_records((ROOT / "examples/records" / name).open()) if r["message_type"] == "Position"
        )

    buoy, weather = first("harbour-buoy.records.jsonl"), first("weather-station.records.jsonl")
    problems: list[str] = []

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    watched: set[str] = set()
    config = ecn.load_config()

    async def watch(ready: asyncio.Event) -> None:
        async with ecn.ECNClient(config.model_copy(update={"integration_name": "oeg-live-watcher"})) as client:
            stream = await client.entities.watch(
                integrations={INTEGRATION},
                categories={ecn.EntityCategory.DEVICE, ecn.EntityCategory.TRACK},
                delivery=ecn.DeliveryPolicy.FIFO,
            )
            ready.set()
            try:
                async for event in stream:
                    watched.add(str(event.entity.id))
            finally:
                await stream.aclose()

    async def start_watcher() -> asyncio.Future[None]:
        ready = asyncio.Event()
        task = asyncio.ensure_future(watch(ready))
        await asyncio.wait_for(ready.wait(), 15)
        return task

    watcher = asyncio.run_coroutine_threadsafe(start_watcher(), loop).result(30)

    def run(mappings: Path, label: str, repeat_for: float, records: list[dict]) -> None:
        adapter = PicogridECN({"name": "ecn", "adapter": "picogrid-ecn"})
        gateway = Gateway(prepare_mappings([mappings], [adapter]), [Destination(adapter, None, 10)])
        gateway.start()
        end = time.monotonic() + repeat_for
        queued = 0
        while True:
            for source in records:
                outcome = gateway.handle(next(shift_to(iter([source]), time.time_ns())))
                queued += outcome == "sent"
                print(f"{label}: {outcome}", flush=True)
                if outcome != "sent":
                    problems.append(f"{label}: a record was not sent ({outcome})")
            if time.monotonic() >= end:
                break
            time.sleep(interval)
        deadline = time.monotonic() + 10
        while gateway.destinations[0].counts.sent < queued and time.monotonic() < deadline:
            time.sleep(0.1)
        if gateway.destinations[0].counts.sent < queued:
            problems.append(f"{label}: {queued - gateway.destinations[0].counts.sent} sends did not complete")
        for receipt in adapter.receipts:
            if receipt.qos == 1 and receipt.delivery_phase.value != "broker_accepted":
                problems.append(f"{label}: a QoS 1 publish was not acknowledged ({receipt.delivery_phase.value})")
        print(f"{label}: receipts", sorted({(r.qos, r.delivery_phase.value) for r in adapter.receipts}), flush=True)
        gateway.close()

    run(ROOT / "mappings", "active", seconds, [buoy, weather])
    for uid, what in (("7", "buoy"), ("KX7", "weather station")):
        if str(entity_uuid(INTEGRATION, uid)) not in watched:
            problems.append(f"the watching client did not receive the {what} entity")
    loop.call_soon_threadsafe(watcher.cancel)
    # The cleanup: the SDK has no delete, so each example entity is published INACTIVE.
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("harbour-buoy.position.yml", "weather-station.position.yml"):
            mapping = yaml.safe_load((ROOT / "mappings" / name).read_text())
            mapping["picogrid_ecn"] = {**(mapping.get("picogrid_ecn") or {}), "status": "INACTIVE"}
            (Path(tmp) / name).write_text(yaml.safe_dump(mapping))
        run(Path(tmp), "inactive", 0, [buoy, weather])
    for problem in problems:
        print("FAIL:", problem)
    return 1 if problems else 0


def verify(seconds: float, interval: float, insecure: bool) -> int:
    base = os.environ["ORIONX_URL"].rstrip("/")
    if not base.startswith("https://"):
        sys.exit("ORIONX_URL must be an https:// URL: the login is sent with every request")
    url = base + "/api/state"
    token = base64.b64encode(f"{os.environ['ORIONX_USER']}:{os.environ['ORIONX_PASSWORD']}".encode()).decode()
    context = ssl.create_default_context(cafile=os.environ.get("ORIONX_CA"))
    if insecure:  # explicit opt-in only: nothing then proves whose OrionX this is
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    def mine() -> list[dict]:
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})  # noqa: S310 - https only
        with urllib.request.urlopen(request, timeout=10, context=context) as response:  # noqa: S310
            state = json.load(response)
        return [t for t in state.get("tracks", []) if t.get("source_integration") == INTEGRATION]

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        tracks = mine()
        if tracks:
            keep = ("id", "affiliation", "lat", "lon", "alt", "vn", "ve", "age_s", "source_integration")
            print("appeared:", json.dumps({k: tracks[0].get(k) for k in keep}), flush=True)
            return 0
        time.sleep(interval)
    print("FAIL: no track from", INTEGRATION, "in OrionX")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("role", choices=["publish", "verify"])
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--insecure", action="store_true", help="verify: do not check OrionX's certificate")
    args = parser.parse_args()
    if args.role == "publish":
        sys.exit(publish(args.seconds, args.interval))
    sys.exit(verify(args.seconds, args.interval, args.insecure))
