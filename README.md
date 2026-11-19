# open-entity-gateway

**Bridges between the goat fabric and vendor systems.** Each bridge is its own package that speaks one vendor's software
kit: Anduril Lattice and Picogrid ECN today, and the next vendor's tomorrow.

Systems that speak an open standard (TAK servers over Cursor on Target, SAPIENT controllers, ASTERIX consumers) are reached
through [goat-transformers](https://github.com/USAREUR-AF/goat-transformers), not through this repository. The scope is set
by goatcore [ADR 1255](https://github.com/USAREUR-AF/goatcore/pull/1084) (proposed).

<p align="center"><img src="docs/img/where-it-fits.svg" width="820" alt="Tracks on the goat fabric reach outside systems three ways. Systems that speak an open standard, such as TAK servers, SAPIENT controllers and ASTERIX consumers, are reached through goat-transformers and its transport adapters. Vendor systems reached through the vendor's own software kit, such as Anduril Lattice and Picogrid ECN, are reached through this repository's vendor bridges. Receivers that cannot join the mesh are reached through an octo-pod's release gate and egress."></p>

## What is here, and what is leaving

This repository began as a general gateway with its own track model, encoders and senders for open standards. Those parts
duplicate goat-transformers and are moving there. Each keeps working until goat-transformers passes the same end-to-end test.

<p align="center"><img src="docs/img/inside.svg" width="820" alt="Four tiers. Vendor packages: Lattice, Picogrid ECN, and room for the next vendor. The shared core: the adapter interface and inputs, moving to read goat-transformers' shared model. Moving to goat-transformers: the CoT, SAPIENT and ASTERIX senders and the gateway's own encoders, which keep working until goat-transformers passes the same tests. Withdrawn: the fabric relay destination."></p>

| Part | Status | Where it is going |
|---|---|---|
| Anduril Lattice bridge (`vendors/lattice/`) | built | stays |
| Picogrid ECN bridge (`vendors/picogrid-ecn/`), with a recorded live run against an ECN and OrionX | built, measured | stays |
| Shared core: adapter interface, mapping files, inputs (HTTP, file, ZeroMQ, Zenoh) | built | stays, and will read goat-transformers' shared model instead of its own track type |
| Cursor on Target sender (UDP, multicast, TCP, TLS), SAPIENT edge-node session, ASTERIX CAT062 sender | built, measured | moving to goat-transformers `adapters/` |
| Its own CoT, SAPIENT and CAT062 encoders | built | replaced by goat-transformers packs (already selectable: `encoder: goat-transformers`) |
| `fabric` destination, which republished encoded tracks for an octo-pod to relay | withdrawn | no replacement; a pod delivers directly from whatever publishes the data |

## Adding a vendor

A bridge is a package under `vendors/<vendor>/` with its own `pyproject.toml`, tests and README. It implements the adapter
interface in `src/open_entity_gateway/adapter.py` and imports its vendor's SDK, which the user installs under the vendor's
licence. Core CI installs no vendor SDK, and a bridge's tests skip unless its SDK is present. What an unbuilt vendor bridge
would need, and companies with no published interface: [docs/vendors.md](docs/vendors.md). Owners, licences and sources
of the interfaces researched: [docs/interfaces.md](docs/interfaces.md). Vendors are listed
alphabetically; their presence is not a preference, and bridges for other vendors are welcome.

## Run a bridge

Python 3.11 or later.

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements/runtime.txt
pip install --no-deps -e . -e vendors/picogrid-ecn     # or vendors/lattice; then install that vendor's SDK
open-entity-gateway check --config gateway.yml
open-entity-gateway zenoh --config gateway.yml --zenoh-config fabric.json5 --key 'sensors/*/tracks'
```

A configuration names each destination; there is no default ([examples/gateway.yml](examples/gateway.yml)). The `zenoh`
command subscribes only to the key expressions given with `--key` and refuses a key made only of wildcards. Each bridge's
options: [docs/adapters/lattice.md](docs/adapters/lattice.md), [docs/adapters/picogrid-ecn.md](docs/adapters/picogrid-ecn.md).
Mapping files: [docs/mapping.md](docs/mapping.md).

## Tests

```bash
pip install --require-hashes -r requirements/runtime.txt -r requirements/dev.txt -r requirements/tools.txt
python tools/fetch-cot-xsd.py && python tools/fetch-sapient-proto.py
python -m pytest tests vendors/lattice/tests
```

## Neutrality

Named products and companies appear only to state which published interface a bridge speaks, as described by their owners.
No endorsement by or of any company, product or government is implied. All trademarks belong to their owners. A bridge
requires the user to obtain and accept that vendor's licence; this project distributes no vendor software.

## Licence

Apache License 2.0; see [LICENSE](LICENSE), [NOTICE](NOTICE) and [INTENT.md](INTENT.md). Part of
[goat](https://github.com/USAREUR-AF/goatcore).
