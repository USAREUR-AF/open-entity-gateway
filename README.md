# open-entity-gateway

**Turns decoded messages into tracks and publishes them over open interface standards: Cursor-on-Target, SAPIENT and ASTERIX category 062. Optional adapters reach vendor systems.**

<p align="center"><img src="docs/img/gateway-flow.svg" width="820" alt="Records or Canonical Track v2 objects arrive over HTTP, from a file or ZeroMQ; a mapping file picks which become tracks; each track goes only to the destinations the configuration lists. A record that cannot fill its mapping is refused and nothing is sent."></p>

- **Reads records.** A record is a JSON or CBOR object with one message's decoded fields under `message`, plus `subsystem`, `message_type` and `timestamp`. [first-bite](https://github.com/USAREUR-AF/first-bite) writes them; any program writing the same shape can feed the gateway.
- **Maps them.** A small YAML file per message says which records become tracks and which fields fill them ([docs/mapping.md](docs/mapping.md)).
- **Sends only where you say.** There is no default destination. A configuration that lists none stops the gateway at start-up.

Where this is heading, as the one way tracks leave the goat fabric for systems outside it: [docs/design/track-egress.md](docs/design/track-egress.md) (designed, not built).

## Interfaces

| Interface | Kind | Status | Tested against |
|---|---|---|---|
| Cursor-on-Target 2.0 (UDP, multicast, TCP, TLS) | open standard | built, measured | OpenTAKServer; MITRE schemas |
| SAPIENT, BSI Flex 335 v2.0 | open standard | built, measured | Dstl Apex SAPIENT middleware 4.2.0 |
| ASTERIX category 062 | open standard | built, measured | asterix4py decoder, field by field |
| Anduril Lattice SDK | vendor adapter | built | the SDK with a local stub |
| Picogrid ECN SDK | vendor adapter | built, measured | the SDK's offline mock |

**Not built yet.** Open standards: STANAG 4676, OGC SensorThings, OARIS 2.0, C2SIM, NGVA. Vendor interfaces: Aaronia AARTOS, Auterion SDK, BAE Systems INTeACT, Palantir platform SDKs, Picogrid Legion, Rheinmetall TacticalAPI, Skydio Cloud API.

Owners, licences, sources and dates for every row: [docs/interfaces.md](docs/interfaces.md). What each built adapter sends: [docs/adapters/](docs/adapters/). What an unbuilt vendor adapter would need, and companies with no published interface: [docs/vendors.md](docs/vendors.md). Controlled military data links are out of scope: Link 16, JREAP, VMF, ADatP-3, JC3IEDM.

Status words: **built** means the code exists and its unit tests pass; **measured** means a command ran against the named test target. Vendors are listed alphabetically and their presence is not an endorsement or a preference; integrations for other vendors are welcome.

## Neutrality

This project implements publicly available interface standards. Named products and companies appear only to state which published standard or interface an adapter speaks, as described by their owners at the cited sources. No endorsement by or of any company, product or government is implied. All trademarks belong to their owners. Optional vendor adapters require the user to obtain and accept that vendor's licence; this project distributes no vendor software.

## Run it

Python 3.11 or later.

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements/runtime.txt
pip install --no-deps -e .
python tools/fetch-sapient-proto.py      # only for the SAPIENT adapter; needs requirements/tools.txt
```

Print what each adapter would send for first-bite's example records, sending nothing:

```bash
open-entity-gateway print --mapping mappings --adapter cot examples/records/weather-station.records.jsonl
```

```xml
<event version="2.0" uid="KX7" type="a-f-G" time="2025-06-15T15:06:43Z" start="2025-06-15T15:06:43Z" stale="2025-06-15T15:16:43Z" how="m-g" access="Undefined"><point lat="64.8378" lon="-147.7164" hae="136.5" ce="9999999" le="9999999"/><detail><contact callsign="KX7"/></detail></event>
```

To send, write a configuration that names each destination ([examples/gateway.yml](examples/gateway.yml)), check it, then take records from first-bite's `rest` sink or from a file:

```bash
open-entity-gateway check  --config gateway.yml
open-entity-gateway serve  --config gateway.yml --listen 127.0.0.1:5001
open-entity-gateway replay --config gateway.yml examples/records/weather-station.records.jsonl --shift-to-now
```

`serve` answers each POST: `204` the record was queued for every destination or no mapping matches it; `422` a mapping matches and the record cannot fill it; `400`, `404`, `411`, `413`, `415` for a request that is not a record posted to the path. It speaks plain HTTP and binds loopback by default. The `zmq` command reads first-bite's ZeroMQ publisher socket instead (`pip install pyzmq`).

## Mapping files

One YAML file per message. The keys `uid`, `type`, `position`, `stale_minutes` and `form` are first-bite's `cot:` mapping keys and mean the same, so the block a first-bite subscriber carries under `cot:` is a mapping file's core unchanged. [docs/mapping.md](docs/mapping.md) gives every key, the value forms, and where the gateway differs from first-bite.

```yaml
match: { subsystem: WX, message_type: Position, dictionary: weather-station }
uid: [station_id]
type: { value: "a-f-G" }
position: { geodetic: [latitude, longitude, altitude] }
stale_minutes: 10
time: capture              # default: the record's capture time
name: { field: station_id }
```

## Tests

```bash
pip install --require-hashes -r requirements/runtime.txt -r requirements/dev.txt -r requirements/tools.txt
python tools/fetch-cot-xsd.py && python tools/fetch-sapient-proto.py
python -m pytest
```

The unit tests need no server and no vendor SDK. `tests/e2e/run-on-build-host.sh` starts OpenTAKServer and Dstl's Apex in containers and runs `tests/e2e/` against them. The vendor packages' tests are under `vendors/*/tests/`; each skips unless its vendor SDK is installed, and CI installs none.

## Repository

| Path | What |
|---|---|
| `src/open_entity_gateway/` | the core (inputs, mapping, gateway, adapter interface) and the open-standard adapters |
| `vendors/lattice/`, `vendors/picogrid-ecn/` | optional vendor adapters, each its own package |
| `mappings/` | example mapping files, for first-bite's weather-station dictionary and hello-galaxy-first-bite's harbour-buoy dictionary |
| `examples/` | example records and an example configuration |
| `tools/` | pinned fetchers for the SAPIENT protos and the CoT schemas; the cotify test-vector generator |
| `tests/` | unit tests; `tests/e2e/` against servers in containers; `tests/vectors/` events first-bite wrote |
| `docs/` | mapping, interfaces, adapters, vendors, handling rules, relationship to first-bite and goat, the track-egress design |

## Licence

Apache License 2.0; see [LICENSE](LICENSE), [NOTICE](NOTICE) and [INTENT.md](INTENT.md). Third-party material the gateway fetches or depends on keeps its own licence; [NOTICE](NOTICE) lists it. Part of [goat](https://github.com/USAREUR-AF/goatcore): see [docs/first-bite-and-goat.md](docs/first-bite-and-goat.md).
