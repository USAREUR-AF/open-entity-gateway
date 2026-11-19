"""Gateway, fabric, octo-pod downlink bridge, egress arm, UDP receiver, on one host.

    python tests/e2e/octo-pod-egress/run.py <tls dir> <work dir> <octo-pod arms/target/release>

run.sh prepares the three arguments. One in-process Zenoh router, with mutual TLS,
stands in for both the fabric router and the pod's pen. The gateway replays the example
tracks to three fabric destinations: CoT (goat-transformers encoder) and ASTERIX CAT062
on two keys the pod's downlink allow list names, and CoT on a third key it does not
name. The pod's downlink bridge republishes the allowed keys under `downlink/<key>`, and
one egress arm per key delivers each sample as a UDP datagram. A client on the fabric
records what the gateway published. It passes when every datagram on an allowed key
equals a published payload, byte for byte and in order, and nothing arrives for the
key the allow list leaves out.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import yaml
import zenoh

ROOT = Path(__file__).resolve().parents[3]
PORT = 17447
KEYS = {
    "cot": "release/lane-e2e/tracks/cot",
    "cat062": "release/lane-e2e/tracks/cat062",
    "unreleased": "release/lane-e2e/tracks/unreleased",
}
RELEASED = ("cot", "cat062")
UDP = {"cot": 18610, "cat062": 18611, "unreleased": 18612}


def zenoh_config(tls: Path, mode: str, name: str) -> dict[str, Any]:
    side = "listen" if mode == "router" else "connect"
    block: dict[str, Any] = {
        "root_ca_certificate": str(tls / "ca.pem"),
        "enable_mtls": True,
        f"{side}_certificate": str(tls / f"{name}-cert.pem"),
        f"{side}_private_key": str(tls / f"{name}-key.pem"),
    }
    return {
        "mode": mode,
        "scouting": {"multicast": {"enabled": False}},
        side: {"endpoints": [f"tls/127.0.0.1:{PORT}"]},
        "transport": {"link": {"tls": block}},
    }


def main(tls: Path, work: Path, arms: Path) -> int:
    def open_session(mode: str, name: str) -> zenoh.Session:
        return zenoh.open(zenoh.Config.from_json5(json.dumps(zenoh_config(tls, mode, name))))

    router = open_session("router", "router")
    published: dict[str, list[bytes]] = {k: [] for k in KEYS}
    tap = open_session("client", "tap")

    def record(k: str) -> Any:
        return lambda sample: published[k].append(bytes(sample.payload.to_bytes()))

    subscribers = [tap.declare_subscriber(key, record(k)) for k, key in KEYS.items()]

    received: dict[str, list[bytes]] = {k: [] for k in KEYS}
    stop = threading.Event()

    def listen(k: str, sock: socket.socket) -> None:
        while not stop.is_set():
            try:
                received[k].append(sock.recv(65535))
            except TimeoutError:
                pass

    for k, port in UDP.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", port))
        sock.settimeout(0.2)
        threading.Thread(target=listen, args=(k, sock), daemon=True).start()

    endpoint = f"tls/127.0.0.1:{PORT}"
    procs: list[subprocess.Popen[bytes]] = []
    logs: list[IO[str]] = []

    def spawn(name: str, binary: str, env: dict[str, str]) -> None:
        log = (work / f"{name}.log").open("w")
        logs.append(log)
        procs.append(subprocess.Popen([str(arms / binary)], env={**os.environ, **env}, stdout=log, stderr=log))

    spawn(
        "downlink-bridge",
        "downlink-bridge",
        {
            "DOWNLINK_FABRIC_ENDPOINT": endpoint,
            "DOWNLINK_FABRIC_CERT": str(tls / "bridge-cert.pem"),
            "DOWNLINK_FABRIC_KEY": str(tls / "bridge-key.pem"),
            "DOWNLINK_FABRIC_ROOT_CA": str(tls / "ca.pem"),
            "DOWNLINK_VERIFY_NAME_FABRIC": "false",
            "DOWNLINK_PEN_ENDPOINT": endpoint,
            "DOWNLINK_PEN_CERT": str(tls / "bridge-cert.pem"),
            "DOWNLINK_PEN_KEY": str(tls / "bridge-key.pem"),
            "DOWNLINK_PEN_ROOT_CA": str(tls / "ca.pem"),
            "DOWNLINK_ALLOW": ",".join(KEYS[k] for k in RELEASED),
        },
    )
    for k, key in KEYS.items():
        audit = work / f"audit-{k}"
        audit.mkdir()
        spawn(
            f"egress-{k}",
            "egress-arm",
            {
                "EGRESS_ID": f"oeg-e2e-{k}",
                "EGRESS_KEYS": key,
                "EGRESS_DEST": f"udp://127.0.0.1:{UDP[k]}",
                "EGRESS_ALLOW_DEST": f"127.0.0.1:{UDP[k]}",
                "EGRESS_ENDPOINT": endpoint,
                "EGRESS_CERT": str(tls / "arm-cert.pem"),
                "EGRESS_KEY": str(tls / "arm-key.pem"),
                "EGRESS_ROOT_CA": str(tls / "ca.pem"),
                "EGRESS_AUDIT_DIR": str(audit),
            },
        )
    time.sleep(3)

    client = work / "fabric-client.json5"
    client.write_text(json.dumps(zenoh_config(tls, "client", "gateway")))
    encodings = {
        "cot": {"adapter": "cot", "encoder": "goat-transformers"},
        "cat062": {"adapter": "asterix-cat062", "sac": 25, "sic": 10},
        "unreleased": {"adapter": "cot"},
    }
    config = {
        "mappings": [str(ROOT / "mappings")],
        "destinations": [
            {"name": f"to-pod-{k}", "adapter": "fabric", "key": KEYS[k], "zenoh_config": str(client), "encode": enc}
            for k, enc in encodings.items()
        ],
    }
    (work / "gateway.yml").write_text(yaml.safe_dump(config))
    replay = [sys.executable, "-m", "open_entity_gateway", "replay", "--config", str(work / "gateway.yml")]
    replay += ["--shift-to-now", "--rate", "4", str(ROOT / "examples/tracks/example.tracks.jsonl")]
    gateway = subprocess.run(replay, capture_output=True, text=True, timeout=120, check=False)
    time.sleep(3)

    stop.set()
    for proc in procs:
        proc.terminate()
    for proc in procs:
        proc.wait(10)
    for log in logs:
        log.close()
    for subscriber in subscribers:
        subscriber.undeclare()
    tap.close()
    router.close()

    report: dict[str, Any] = {"gateway_exit": gateway.returncode}
    ok = gateway.returncode == 0
    for k in KEYS:
        sent, got = published[k], received[k]
        expected = sent if k in RELEASED else []
        ok &= len(sent) > 0 and got == expected
        report[k] = {
            "released": k in RELEASED,
            "published": len(sent),
            "delivered": len(got),
            "as_expected": got == expected,
        }
    print(json.dumps(report, indent=2))
    if gateway.returncode:
        print(gateway.stdout + gateway.stderr)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
