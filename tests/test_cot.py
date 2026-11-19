"""Cursor-on-Target: the event matches first-bite's, validates against the
MITRE public-release schemas, and arrives over UDP, multicast, TCP and TLS."""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from open_entity_gateway.adapters.cot import CursorOnTarget
from open_entity_gateway.adapters.cot.event import event_xml
from open_entity_gateway.gateway import Destination, Gateway
from open_entity_gateway.mapping import Mapping, load_mappings

from .conftest import MAPPINGS, ROOT

pytestmark = pytest.mark.usefixtures("each_encoder")
ACCESS = "the cot-xml pack writes no access attribute, which the MITRE base-event schema requires"

VECTORS = [json.loads(line) for line in (ROOT / "tests" / "vectors" / "cotify.jsonl").read_text().splitlines()]


def as_gateway_mapping(case: dict[str, Any]) -> Mapping:
    document = {"match": {"subsystem": "X", "message_type": "Y"}, **case["mapping"], "time": "received"}
    return Mapping(document, case["case"])


@pytest.mark.parametrize("case", VECTORS, ids=[c["case"] for c in VECTORS])
def test_the_event_is_the_one_first_bite_writes(case: dict[str, Any]) -> None:
    """With `time: received`, the gateway's event is byte for byte the event
    first-bite's cotify writes for the same mapping, record and moment
    (tests/vectors/cotify.jsonl, made by tools/cotify-vectors)."""
    secs, nanos = case["now"]
    track = as_gateway_mapping(case).track(case["record"], received_ns=secs * 10**9 + nanos)
    assert event_xml(track) == case["event"]
    with_access = event_xml(track, "Undefined")
    assert with_access == case["event"].replace(' how="m-g">', ' how="m-g" access="Undefined">', 1)


VALID_TYPES = [c for c in VECTORS if "escaping" not in c["case"]]


@pytest.mark.goat_differs(ACCESS)
@pytest.mark.parametrize("case", VALID_TYPES, ids=[c["case"] for c in VALID_TYPES])
def test_every_gateway_event_validates_against_the_mitre_schema(case: dict[str, Any], cot_schema: Any) -> None:
    secs, nanos = case["now"]
    track = as_gateway_mapping(case).track(case["record"], received_ns=secs * 10**9 + nanos)
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    cot_schema.validate(adapter.encode(track, case["record"], None).decode())


def test_a_type_the_schema_refuses_is_refused_by_the_adapter() -> None:
    """The escaping vector's type holds quotes and brackets: first-bite writes
    it, escaped; the schema's pattern for `type` refuses it, so the gateway
    refuses the record and sends nothing."""
    from open_entity_gateway.record import RecordError

    (case,) = [c for c in VECTORS if "escaping" in c["case"]]
    track = as_gateway_mapping(case).track(case["record"], received_ns=0)
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    with pytest.raises(RecordError, match="CoT type"):
        adapter.encode(track, case["record"], None)


def test_without_access_the_published_schema_refuses_the_event(cot_schema: Any) -> None:
    """The reason the gateway writes `access`: first-bite's event, as written,
    lacks the attribute the published base-event schema requires."""
    import xmlschema

    with pytest.raises(xmlschema.XMLSchemaValidationError, match="missing required attribute 'access'"):
        cot_schema.validate(VECTORS[0]["event"])


@pytest.mark.goat_differs(ACCESS)
def test_every_example_event_validates(
    cot_schema: Any, ws_records: list[dict[str, Any]], buoy_position: dict[str, Any]
) -> None:
    mappings = load_mappings([MAPPINGS], ["asterix", "sapient", "lattice", "picogrid_ecn"])
    adapter = CursorOnTarget({"name": "p", "adapter": "cot", "print": True})
    events = []
    for record in [*ws_records, buoy_position]:
        for m in mappings:
            if m.matches(record):
                events.append(adapter.encode(m.track(record), record, None).decode())
    assert len(events) == 2
    assert '<detail><contact callsign="KX7"/></detail>' in events[0]
    assert '<track course="135" speed="0.35"/>' in events[1]
    for event in events:
        cot_schema.validate(event)


def test_an_event_without_altitude_writes_the_unknown_value(cot_schema: Any, ws_position: dict[str, Any]) -> None:
    m = Mapping(
        {
            "match": {"subsystem": "WX", "message_type": "Position"},
            "uid": ["station_id"],
            "type": {"value": "a-f-G"},
            "position": {"geodetic": ["latitude", "longitude"]},
        },
        "t",
    )
    event = event_xml(m.track(ws_position), "Undefined")
    assert 'hae="9999999"' in event
    cot_schema.validate(event)


def _gateway(spec: dict[str, Any]) -> Gateway:
    adapter = CursorOnTarget(spec)
    mappings = load_mappings([MAPPINGS / "weather-station.position.yml"], ["asterix", "lattice", "picogrid_ecn"])
    gateway = Gateway(mappings, [Destination(adapter, None, 10)])
    return gateway


@pytest.mark.goat_differs(ACCESS)
def test_udp_unicast(ws_records: list[dict[str, Any]], cot_schema: Any) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    gateway = _gateway(
        {"name": "u", "adapter": "cot", "transport": "udp", "address": f"127.0.0.1:{listener.getsockname()[1]}"}
    )
    gateway.start()
    for record in ws_records:
        gateway.handle(record)
    gateway.close()
    datagram, _ = listener.recvfrom(65536)
    assert datagram.startswith(b'<event version="2.0" uid="KX7"')
    cot_schema.validate(datagram.decode())
    assert gateway.destinations[0].counts.sent == 1


@pytest.mark.goat_differs(ACCESS)
def test_udp_multicast(ws_position: dict[str, Any], cot_schema: Any) -> None:
    group = "239.2.3.1"
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("", 0))
    port = listener.getsockname()[1]
    try:
        listener.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton("127.0.0.1")),
        )
    except OSError as error:
        pytest.skip(f"this host cannot join a multicast group on loopback: {error}")
    listener.settimeout(5)
    gateway = _gateway(
        {
            "name": "m",
            "adapter": "cot",
            "transport": "udp",
            "address": f"{group}:{port}",
            "ttl": 1,
            "interface": "127.0.0.1",
        }
    )
    gateway.start()
    gateway.handle(ws_position)
    gateway.close()
    datagram, _ = listener.recvfrom(65536)
    cot_schema.validate(datagram.decode())


def _accept_all(server: socket.socket, into: list[bytes], wrap: ssl.SSLContext | None = None) -> None:
    connection, _ = server.accept()
    if wrap is not None:
        connection = wrap.wrap_socket(connection, server_side=True)
    with connection:
        while chunk := connection.recv(65536):
            into.append(chunk)


def _events(stream: bytes) -> list[str]:
    """Split a TAK stream as atak-civ takproto/README.txt says: after each `</event>`."""
    parts = stream.decode().split("</event>")
    return [p + "</event>" for p in parts if p.strip()]


@pytest.mark.goat_differs(ACCESS)
def test_tcp_stream_has_a_declaration_before_each_event(ws_position: dict[str, Any], cot_schema: Any) -> None:
    server = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []
    thread = threading.Thread(target=_accept_all, args=(server, received), daemon=True)
    thread.start()
    gateway = _gateway(
        {"name": "t", "adapter": "cot", "transport": "tcp", "address": f"127.0.0.1:{server.getsockname()[1]}"}
    )
    gateway.start()
    gateway.handle(ws_position)
    gateway.handle(ws_position)
    gateway.close()
    thread.join(5)
    events = _events(b"".join(received))
    assert len(events) == 2
    for event in events:
        assert event.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<event ')
        cot_schema.validate(event.split("\n", 1)[1])


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl to make test certificates")
    d = tmp_path_factory.mktemp("pki")

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "1",
        "-subj",
        "/CN=test authority",
    )
    (d / "ext").write_text("subjectAltName=DNS:tak.test,IP:127.0.0.1\n")
    for name in ("server", "client"):
        run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
            "-subj",
            f"/CN={name}",
        )
        run(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            f"{name}.pem",
            "-days",
            "1",
            "-extfile",
            "ext",
        )
    return d


@pytest.mark.goat_differs(ACCESS)
def test_tls_with_a_client_certificate(ws_position: dict[str, Any], certificates: Path, cot_schema: Any) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    context.load_verify_locations(certificates / "ca.pem")
    context.verify_mode = ssl.CERT_REQUIRED
    server = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []
    thread = threading.Thread(target=_accept_all, args=(server, received, context), daemon=True)
    thread.start()
    gateway = _gateway(
        {
            "name": "tls",
            "adapter": "cot",
            "transport": "tcp",
            "address": f"127.0.0.1:{server.getsockname()[1]}",
            "tls": {
                "ca_file": str(certificates / "ca.pem"),
                "client_cert_file": str(certificates / "client.pem"),
                "client_key_file": str(certificates / "client.key"),
                "server_name": "tak.test",
            },
        }
    )
    gateway.start()
    gateway.handle(ws_position)
    gateway.close()
    thread.join(5)
    (event,) = _events(b"".join(received))
    cot_schema.validate(event.split("\n", 1)[1])
    assert gateway.destinations[0].counts.sent == 1


def test_tls_refuses_a_server_the_authority_did_not_sign(
    ws_position: dict[str, Any], certificates: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(tmp_path / "o.key"),
            "-out",
            str(other),
            "-days",
            "1",
            "-subj",
            "/CN=other",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "server.pem", certificates / "server.key")
    server = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []

    def serve() -> None:
        try:
            _accept_all(server, received, context)
        except (ssl.SSLError, OSError):
            pass

    threading.Thread(target=serve, daemon=True).start()
    adapter = CursorOnTarget(
        {
            "name": "x",
            "adapter": "cot",
            "transport": "tcp",
            "address": f"127.0.0.1:{server.getsockname()[1]}",
            "tls": {"ca_file": str(other), "server_name": "tak.test"},
        }
    )
    assert adapter.transport is not None
    adapter.transport.closing.set()  # one attempt only
    with pytest.raises(Exception, match="closed before a connection"):
        adapter.send(b"<event/>")
    assert received == []


def test_time_forms(ws_position: dict[str, Any], cot_schema: Any) -> None:
    m = load_mappings([MAPPINGS / "weather-station.position.yml"], ["asterix", "lattice", "picogrid_ecn"])[0]
    record = {**ws_position, "timestamp": {"secs_since_epoch": 1_750_000_003, "nanos_since_epoch": 962_290_094}}
    track = m.track(record)
    z = event_xml(track, "Undefined", "z")
    assert 'time="2025-06-15T15:06:43.96229Z"' in z and 'stale="2025-06-15T15:16:43.96229Z"' in z
    fb = event_xml(track, "Undefined", "first-bite")
    assert 'time="2025-06-15T15:06:43.962290094+00:00"' in fb
    for event in (z, fb):
        cot_schema.validate(event)
    with pytest.raises(Exception, match="time_form"):
        CursorOnTarget({"name": "x", "adapter": "cot", "print": True, "time_form": "local"})


@pytest.mark.goat_differs(ACCESS)
def test_tcp_announces_the_gateway_first(ws_position: dict[str, Any], cot_schema: Any) -> None:
    server = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []
    thread = threading.Thread(target=_accept_all, args=(server, received), daemon=True)
    thread.start()
    gateway = _gateway(
        {
            "name": "t",
            "adapter": "cot",
            "transport": "tcp",
            "address": f"127.0.0.1:{server.getsockname()[1]}",
            "announce": {"uid": "gateway-1", "callsign": "GW", "settle_seconds": 0},
        }
    )
    gateway.start()
    gateway.handle(ws_position)
    gateway.close()
    thread.join(5)
    presence, event = _events(b"".join(received))
    assert 'uid="gateway-1"' in presence and '<contact callsign="GW"/>' in presence
    assert '<__group name="Cyan" role="Team Member"/>' in presence
    assert 'uid="KX7"' in event
    for item in (presence, event):
        cot_schema.validate(item.split("\n", 1)[1])
