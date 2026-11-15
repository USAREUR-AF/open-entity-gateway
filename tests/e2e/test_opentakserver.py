"""Cursor-on-Target to OpenTAKServer, end to end.

A TAK client stand-in (the observer) connects to the server's streaming port
and announces itself. The gateway then sends the weather-station Position
track to the same server. The test passes when the server routes the event
to the observer, the event the observer receives validates against the MITRE
schemas, and its uid, type and point are the gateway's.

Runs when OEG_E2E_TAK (HOST:PORT of the plain TCP input) is set; TLS when
OEG_E2E_TAK_TLS and OEG_E2E_TAK_CA (the server's CA directory) are set too.
tests/e2e/run-on-build-host.sh sets them.
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from open_entity_gateway.adapters.cot import CursorOnTarget
from open_entity_gateway.gateway import Destination, Gateway
from open_entity_gateway.inputs.replay import shift_to
from open_entity_gateway.mapping import load_mappings

from ..conftest import MAPPINGS

TAK = os.environ.get("OEG_E2E_TAK")
TAK_TLS = os.environ.get("OEG_E2E_TAK_TLS")
TAK_CA = os.environ.get("OEG_E2E_TAK_CA")
pytestmark = pytest.mark.e2e


def split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host, int(port)


def announce(sock: socket.socket, uid: str) -> None:
    # OpenTAKServer opens a broker channel for each new connection; an event
    # that arrives before it is open is lost (measured), so wait first.
    time.sleep(2)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    sock.sendall(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<event version="2.0" uid="{uid}" type="a-f-G-U-C" '
        f'time="{now}" start="{now}" stale="{stale}" how="h-e"><point lat="0" lon="0" hae="0" ce="9999999" '
        f'le="9999999"/><detail><contact callsign="{uid}" endpoint="*:-1:stcp"/>'
        f'<__group name="Cyan" role="Team Member"/><takv device="test" platform="observer" os="linux" '
        f'version="1"/></detail></event>'.encode()
    )


def read_until(sock: socket.socket, needle: bytes, seconds: float) -> bytes:
    sock.settimeout(1.0)
    data = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            continue
        if not chunk:
            break
        data += chunk
        if needle in data and b"</event>" in data[data.index(needle) :]:
            break
    return data


def received_event(stream: bytes, uid: str) -> str:
    text = stream.decode(errors="replace")
    start = text.rfind("<event", 0, text.index(f'uid="{uid}"'))
    end = text.index("</event>", start) + len("</event>")
    return text[start:end]


def send_position(spec: dict[str, Any], ws_position: dict[str, Any]) -> CursorOnTarget:
    (record,) = list(shift_to(iter([ws_position]), time.time_ns()))
    adapter = CursorOnTarget(spec)
    mappings = load_mappings([MAPPINGS / "weather-station.position.yml"], ["asterix", "lattice", "picogrid_ecn"])
    gateway = Gateway(mappings, [Destination(adapter, None, 10)])
    gateway.start()
    assert gateway.handle(record) == "sent"
    time.sleep(1)
    gateway.close()
    assert gateway.destinations[0].counts.sent == 1
    return adapter


def check(event: str, cot_schema: Any) -> None:
    import xml.etree.ElementTree as ET

    cot_schema.validate(event)
    root = ET.fromstring(event)
    point = root.find("point")
    assert point is not None
    assert (root.get("uid"), root.get("type")) == ("KX7", "a-f-G")
    assert (float(point.get("lat", "")), float(point.get("lon", "")), float(point.get("hae", ""))) == (
        64.8378,
        -147.7164,
        136.5,
    )


def gateway_presence() -> dict[str, str]:
    """A fresh uid for each connection: OpenTAKServer ties a uid to the user
    and the input it first came from."""
    uid = f"open-entity-gateway-e2e-{uuid.uuid4().hex[:8]}"
    return {"uid": uid, "callsign": uid}


@pytest.mark.skipif(not TAK, reason="OEG_E2E_TAK is not set")
@pytest.mark.parametrize(
    ("declaration", "announced", "time_form", "routed"),
    [(True, True, "z", True), (False, True, "z", True), (True, False, "z", False), (True, True, "first-bite", False)],
    ids=["declaration+announce", "bare-events+announce", "without-announce", "first-bite-time-form"],
)
def test_tcp_to_opentakserver(
    declaration: bool, announced: bool, time_form: str, routed: bool, ws_position: dict[str, Any], cot_schema: Any
) -> None:
    """The last two cases record what was measured: OpenTAKServer routes no
    event from a connection that has not announced itself, and none whose
    times are written as first-bite writes them (+00:00, nanoseconds)."""
    assert TAK
    observer = socket.create_connection(split(TAK), timeout=10)
    announce(observer, f"oeg-observer-{uuid.uuid4().hex[:8]}")
    time.sleep(1)
    host, port = split(TAK)
    spec: dict[str, Any] = {
        "name": "ots",
        "adapter": "cot",
        "transport": "tcp",
        "address": f"{host}:{port}",
        "xml_declaration": declaration,
        "time_form": time_form,
    }
    if announced:
        spec["announce"] = gateway_presence()
    send_position(spec, ws_position)
    stream = read_until(observer, b'uid="KX7"', 15)
    observer.close()
    if not routed:
        assert b'uid="KX7"' not in stream
        return
    assert b'uid="KX7"' in stream, f"OpenTAKServer did not route the event; observer read {len(stream)} bytes"
    event = received_event(stream, "KX7")
    print("received from OpenTAKServer:", event)
    check(event, cot_schema)


def client_certificate(directory: Path, ca: Path) -> tuple[Path, Path]:
    """A client certificate signed by the server's own authority, as the
    server's administrator would issue one. OpenTAKServer takes the
    certificate's CN as the user name and refuses a user it does not have, so
    the CN is a user the server has (its default administrator account)."""
    key, csr, cert = directory / "client.key", directory / "client.csr", directory / "client.pem"
    ca_cert = ca / "ca.pem"
    ca_key = ca / "ca-do-not-share.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={os.environ.get('OEG_E2E_TAK_TLS_USER', 'administrator')}",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-passin",
            f"pass:{os.environ.get('OEG_E2E_TAK_CA_PASSWORD', 'atakatak')}",
            "-CAcreateserial",
            "-CAserial",
            str(directory / "ca.srl"),
            "-out",
            str(cert),
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.mark.skipif(not (TAK and TAK_TLS and TAK_CA), reason="OEG_E2E_TAK_TLS and OEG_E2E_TAK_CA are not set")
def test_tls_to_opentakserver(ws_position: dict[str, Any], cot_schema: Any, tmp_path: Path) -> None:
    assert TAK and TAK_TLS and TAK_CA
    ca = Path(TAK_CA)
    cert, key = client_certificate(tmp_path, ca)
    observer = socket.create_connection(split(TAK), timeout=10)
    announce(observer, f"oeg-observer-{uuid.uuid4().hex[:8]}")
    time.sleep(1)
    host, port = split(TAK_TLS)
    adapter = send_position(
        {
            "name": "ots-tls",
            "adapter": "cot",
            "transport": "tcp",
            "address": f"{host}:{port}",
            "tls": {
                "ca_file": str(ca / "ca.pem"),
                "client_cert_file": str(cert),
                "client_key_file": str(key),
                "server_name": os.environ.get("OEG_E2E_TAK_TLS_NAME", "opentakserver"),
            },
            "announce": gateway_presence(),
        },
        ws_position,
    )
    assert adapter.transport is not None
    stream = read_until(observer, b'uid="KX7"', 20)
    observer.close()
    assert b'uid="KX7"' in stream, "OpenTAKServer did not route the event sent over TLS"
    check(received_event(stream, "KX7"), cot_schema)


__all__ = ["ssl"]
