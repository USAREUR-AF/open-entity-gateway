"""A live check, not a pytest test: publish a track to an ECN, see it in OrionX.

Two roles, run on two machines, so no credential is passed to the unit.

  publish  runs where the ECN's broker is reachable (the SDK reads its ECN_*
           variables or a profile; see docs/adapters/picogrid-ecn.md). It
           publishes the harbour-buoy example track with its time moved to
           now every few seconds for a while, then publishes the example
           entities INACTIVE as cleanup (the SDK has no delete).

  verify   runs anywhere that reaches OrionX. It polls OrionX's /api/state
           until a track from the test integration appears and prints it.
           OrionX's address and login come from ORIONX_URL, ORIONX_USER and
           ORIONX_PASSWORD; none is stored here.

    python vendors/picogrid-ecn/tests/live_orionx.py publish --seconds 120
    python vendors/picogrid-ecn/tests/live_orionx.py verify --seconds 150

The check passes if verify prints "appeared". It writes test entities to the ECN.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INTEGRATION = "open-entity-gateway-test"


def publish(seconds: float, interval: float) -> int:
    import yaml
    from open_entity_gateway_picogrid_ecn import PicogridECN

    from open_entity_gateway.gateway import Destination, Gateway, prepare_mappings
    from open_entity_gateway.inputs.replay import read_records, shift_to

    def first(name: str) -> dict:
        return next(
            r for r in read_records((ROOT / "examples/records" / name).open()) if r["message_type"] == "Position"
        )

    buoy, weather = first("harbour-buoy.records.jsonl"), first("weather-station.records.jsonl")

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
            if time.monotonic() >= end:
                break
            time.sleep(interval)
        deadline = time.monotonic() + 10
        while gateway.destinations[0].counts.sent < queued and time.monotonic() < deadline:
            time.sleep(0.1)
        print(
            f"{label}: receipts",
            [(r.qos, r.delivery_phase.value) for r in adapter.receipts[-len(records) :]],
            flush=True,
        )
        gateway.close()

    run(ROOT / "mappings", "active", seconds, [buoy])
    # The cleanup: the SDK has no delete, so each example entity is published INACTIVE.
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("harbour-buoy.position.yml", "weather-station.position.yml"):
            mapping = yaml.safe_load((ROOT / "mappings" / name).read_text())
            mapping["picogrid_ecn"] = {**(mapping.get("picogrid_ecn") or {}), "status": "INACTIVE"}
            (Path(tmp) / name).write_text(yaml.safe_dump(mapping))
        run(Path(tmp), "inactive", 0, [buoy, weather])
    return 0


def verify(seconds: float, interval: float) -> int:
    base = os.environ["ORIONX_URL"].rstrip("/")
    if not base.startswith("https://"):
        sys.exit("ORIONX_URL must be an https:// URL: the login is sent with every request")
    url = base + "/api/state"
    token = base64.b64encode(f"{os.environ['ORIONX_USER']}:{os.environ['ORIONX_PASSWORD']}".encode()).decode()
    context = ssl.create_default_context()
    context.check_hostname = False
    # Lax verification on purpose: a unit presents its own certificate, which no public CA signed.
    # The connection is still TLS, so the login is not sent in the clear.
    context.verify_mode = ssl.CERT_NONE

    def mine() -> list[dict]:
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})  # noqa: S310 - the operator's own https URL
        with urllib.request.urlopen(request, timeout=10, context=context) as response:  # noqa: S310
            state = json.load(response)
        return [t for t in state.get("tracks", []) if t.get("source_integration") == INTEGRATION]

    appeared = False
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        tracks = mine()
        if tracks:
            appeared = True
            keep = ("id", "affiliation", "lat", "lon", "alt", "vn", "ve", "age_s", "source_integration")
            print("appeared:", json.dumps({k: tracks[0].get(k) for k in keep}), flush=True)
            break
        time.sleep(interval)
    if not appeared:
        print("FAIL: no track from", INTEGRATION, "in OrionX")
    return 0 if appeared else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("role", choices=["publish", "verify"])
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    sys.exit(publish(args.seconds, args.interval) if args.role == "publish" else verify(args.seconds, args.interval))
