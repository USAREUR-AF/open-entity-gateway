# Cursor-on-Target

The `cot` adapter writes each track as a CoT 2.0 XML event and sends it over UDP (one event per datagram, unicast or multicast) or over a TCP connection, optionally TLS with a client certificate. The event is the one first-bite's cot transform writes, with the additions below.

| Part | Status |
|---|---|
| event from a track; every event checked against the MITRE public-release schemas | built; measured in `tests/test_cot.py` |
| byte-for-byte equality with first-bite's event for the same mapping, record and moment | measured against 9 events first-bite's cotify wrote (`tests/vectors/`) |
| UDP unicast, multicast; TCP; TLS with a client certificate | measured against listeners on one machine (`tests/test_cot.py`) |
| TCP and TLS to OpenTAKServer, the event routed by the server to a second connected client | measured (`tests/e2e/test_opentakserver.py`) |

## The event

```xml
<event version="2.0" uid="KX7" type="a-f-G" time="2025-06-15T15:06:43Z" start="2025-06-15T15:06:43Z" stale="2025-06-15T15:16:43Z" how="m-g" access="Undefined"><point lat="64.8378" lon="-147.7164" hae="136.5" ce="9999999" le="9999999"/><detail><contact callsign="KX7"/></detail></event>
```

That is the weather-station Position record ([worked example](../mapping.md#worked-example)) through [mappings/weather-station.position.yml](../../mappings/weather-station.position.yml).

What differs from first-bite's event, and why:

| Part | first-bite | gateway |
|---|---|---|
| `access` | not written | written; the destination sets it, default `Undefined`. The base-event schema published in atak-civ (`takcot/mitre`, clarification 4, 01 May 23) makes it required, so an event without it does not validate |
| `time`, `start`, `stale` | `+00:00`, up to nine fractional digits | `time_form: z` (default): `Z`, up to six digits, the form OpenTAKServer's parser takes. `time_form: first-bite` writes first-bite's form; OpenTAKServer refused it (measured) |
| `<detail>` | empty | `<contact callsign>` when the mapping has `name`; `<track course speed>` when it has `motion` |
| `type` | written as given | refused unless it has the form the schema's pattern gives |

## Destination

```yaml
destinations:
  - name: tak-server
    adapter: cot
    transport: tcp                 # or udp
    address: "203.0.113.20:8089"
    tls:                           # tcp only; optional
      ca_file: /etc/open-entity-gateway/tak/ca.pem          # the only authorities trusted
      client_cert_file: /etc/open-entity-gateway/tak/gateway.pem
      client_key_file: /etc/open-entity-gateway/tak/gateway.key
      server_name: tak.example.org # when the address is an IP
    announce:                      # tcp only; see below
      uid: open-entity-gateway-1
      callsign: GATEWAY-1
    access: Undefined
    time_form: z
    xml_declaration: true          # tcp default true; udp default false
    topics: ["WX/Position"]        # optional: SUBSYSTEM/MESSAGE_TYPE this destination takes
```

| Key | Meaning |
|---|---|
| `transport`, `address` | `udp` or `tcp`, and the HOST:PORT its owner chose |
| `ttl`, `interface` | UDP multicast: hop limit, and the address of the interface to send from |
| `source_port` | fixes the local port |
| `tls` | the server is checked against `ca_file` only and must carry `server_name` or the host name |
| `announce` | the gateway's own presence event, sent first on each TCP connection after `settle_seconds` (default 1) and every `interval_seconds` (default 60): `uid`, `callsign`, `group` (default `Cyan`), `role` (default `Team Member`), `type` (default `a-f-G-U-C`), `point` ([latitude, longitude, hae], default 0, 0, 0) |
| `access` | one of the values the schema documents: `Unclassified`, `NATO Unclassified`, `NATO Restricted`, `Confidential`, `NATO Confidential`, `Secret`, `NATO Secret`, `Undefined` |
| `xml_declaration` | each event preceded by `<?xml version="1.0" encoding="UTF-8"?>` and a newline, the framing atak-civ `takproto/README.txt` gives for streams |
| `queue` | payloads held while the connection is away (default 1000); one that finds the queue full is dropped and counted |

A TCP connection is kept open and reopened after 1 s, then 2 s, 4 s, up to 30 s, as first-bite's tcp sink does. On close the gateway stops sending and reads until the server closes or two seconds pass: closing with the server's data unread resets the connection, and over TLS that reset lost the last event at OpenTAKServer (measured).

## Measured against OpenTAKServer

`tests/e2e/run-on-build-host.sh` starts OpenTAKServer from `brian7704/OpenTAKServer-Docker` at commit `30aa20b4` (images `ghcr.io/brian7704/opentakserver:master` and the EUD handlers), with no port published on the host. A stand-in TAK client connects to the plain TCP input (8088) and announces itself; the gateway then sends the weather-station track. The test passes when the server routes the event to the stand-in client, that event validates against the MITRE schemas, and its uid, type and point are the gateway's.

| Case | Result |
|---|---|
| TCP, XML declaration, gateway announced | routed |
| TCP, track events without a declaration (first-bite's framing), gateway announced | routed |
| TCP, gateway not announced | not routed |
| TCP, `time_form: first-bite` | not routed; the server's parser takes `%Y-%m-%dT%H:%M:%S.%fZ` or `%Y-%m-%dT%H:%M:%SZ` only |
| TLS (8089), client certificate signed by the server's authority, CN a user the server has | routed |

OpenTAKServer takes a TLS client certificate's CN as the user name and closes the connection for a user it does not have; the server's administrator issues the certificate for an existing user. A connection's first event, if it arrives before the server has opened that connection's broker channel, is lost; `settle_seconds` waits for it.

## Schemas

`tools/fetch-cot-xsd.py` fetches the base-event, contact and track schemas from atak-civ at commit `9f6893dd` and checks each against its SHA-256. They are MITRE's, approved for public release, and are not kept in this repository.
