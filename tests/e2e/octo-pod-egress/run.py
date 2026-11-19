"""Gateway, fabric, octo-pod downlink bridge, egress arms, receivers, on one host.

    python tests/e2e/octo-pod-egress/run.py <tls dir> <work dir> <octo-pod arms/target/release>

run.sh prepares the three arguments. One in-process Zenoh router, with mutual TLS,
stands in for both the fabric router and the pod's pen. The gateway replays the example
tracks to four fabric destinations: CoT (goat-transformers encoder), ASTERIX CAT062 and
SAPIENT on three keys the pod's downlink allow list names, and CoT on a fourth key it
does not name. The pod's downlink bridge republishes the allowed keys under
`downlink/<key>`. One egress arm per UDP key delivers each sample as a datagram; the
pod's SAPIENT arm, as a `sapient://` destination, registers with a controller on
loopback and delivers each detection report under its own node id. A client on the
fabric records what the gateway published. It passes when every datagram on an allowed
key equals a published payload, byte for byte and in order; every detection report the
controller receives equals a published one, in order, re-addressed to the arm's node;
and nothing arrives for the key the allow list leaves out.
"""

from __future__ import annotations

import json
import os
import socket
import struct
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
    "sapient": "release/lane-e2e/tracks/sapient",
    "unreleased": "release/lane-e2e/tracks/unreleased",
}
RELEASED = ("cot", "cat062", "sapient")
UDP = {"cot": 18610, "cat062": 18611, "unreleased": 18612}
CONTROLLER = 18620
GATEWAY_NODE = "7c6f3a52-1e0b-4f7d-8a39-5b2e9d4c1f60"
CONTROLLER_NODE = "3f1d6c2e-8a4b-4e1f-9c7d-2b5a6e8f0a11"


def sapient_message() -> Any:
    from open_entity_gateway.adapters.sapient._generated.sapient_msg.bsi_flex_335_v2_0 import (
        sapient_message_pb2,
    )

    return sapient_message_pb2.SapientMessage()


class Controller:
    """A SAPIENT controller on loopback: accepts the registration, answering in the byte
    order the node chose, and keeps every message it receives."""

    def __init__(self, port: int) -> None:
        self.server = socket.create_server(("127.0.0.1", port))
        self.server.settimeout(0.2)
        self.messages: list[Any] = []
        self.stop = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except TimeoutError:
                continue
            conn.settimeout(0.2)
            buffer, order = b"", ""
            while not self.stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 4:
                    order = order or ("<" if struct.unpack("<I", buffer[:4])[0] <= 1_000_000 else ">")
                    (size,) = struct.unpack(order + "I", buffer[:4])
                    if len(buffer) < 4 + size:
                        break
                    message = sapient_message()
                    message.ParseFromString(buffer[4 : 4 + size])
                    buffer = buffer[4 + size :]
                    self.messages.append(message)
                    if message.WhichOneof("content") == "registration":
                        ack = sapient_message()
                        ack.timestamp.GetCurrentTime()
                        ack.node_id = CONTROLLER_NODE
                        ack.destination_id = message.node_id
                        ack.registration_ack.acceptance = True
                        body = ack.SerializeToString()
                        conn.sendall(struct.pack(order + "I", len(body)) + body)
            conn.close()

    def of(self, kind: str) -> list[Any]:
        return [m for m in self.messages if m.WhichOneof("content") == kind]


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

    received: dict[str, list[bytes]] = {k: [] for k in UDP}
    stop = threading.Event()
    controller = Controller(CONTROLLER)

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
    for k in UDP:
        key = KEYS[k]
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
    (work / "audit-sapient" / "state").mkdir(parents=True)
    spawn(
        "egress-sapient",
        "sapient",
        {
            "SAPIENT_ARM_ID": "oeg-e2e-sapient",
            "SAPIENT_FEED": "oeg-e2e-sapient",
            "SAPIENT_ROLE": "asm",
            "SAPIENT_TARGET": f"127.0.0.1:{CONTROLLER}",
            "SAPIENT_EGRESS_KEYS": KEYS["sapient"],
            "SAPIENT_ALLOW_DEST": f"127.0.0.1:{CONTROLLER}",
            "SAPIENT_NODE_TYPE": "RADAR",
            "SAPIENT_ENDPOINT": endpoint,
            "SAPIENT_CERT": str(tls / "arm-cert.pem"),
            "SAPIENT_KEY": str(tls / "arm-key.pem"),
            "SAPIENT_ROOT_CA": str(tls / "ca.pem"),
            "SAPIENT_AUDIT_DIR": str(work / "audit-sapient"),
            "SAPIENT_STATE_DIR": str(work / "audit-sapient" / "state"),
        },
    )
    deadline = time.monotonic() + 30
    while not controller.of("registration") and time.monotonic() < deadline:
        time.sleep(0.2)

    client = work / "fabric-client.json5"
    client.write_text(json.dumps(zenoh_config(tls, "client", "gateway")))
    encodings = {
        "cot": {"adapter": "cot", "encoder": "goat-transformers"},
        "cat062": {"adapter": "asterix-cat062", "sac": 25, "sic": 10},
        "sapient": {"adapter": "sapient", "node_id": GATEWAY_NODE},
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
    controller.stop.set()
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
    for k in UDP:
        sent, got = published[k], received[k]
        expected = sent if k in RELEASED else []
        ok &= len(sent) > 0 and got == expected
        report[k] = {
            "released": k in RELEASED,
            "published": len(sent),
            "delivered": len(got),
            "as_expected": got == expected,
        }
    sent_reports = []
    for payload in published["sapient"]:
        message = sapient_message()
        message.ParseFromString(payload)
        sent_reports.append(message.detection_report.SerializeToString())
    delivered = controller.of("detection_report")
    registrations = controller.of("registration")
    arm_node = registrations[0].node_id if registrations else None
    same = [m.detection_report.SerializeToString() for m in delivered] == sent_reports
    readdressed = all(m.node_id == arm_node and m.node_id != GATEWAY_NODE for m in delivered)
    ok &= bool(registrations) and len(sent_reports) > 0 and same and readdressed
    report["sapient"] = {
        "released": True,
        "registered": bool(registrations),
        "published": len(sent_reports),
        "delivered": len(delivered),
        "detection_reports_equal_in_order": same,
        "under_the_arm_node_id": readdressed,
    }
    print(json.dumps(report, indent=2))
    if gateway.returncode:
        print(gateway.stdout + gateway.stderr)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
