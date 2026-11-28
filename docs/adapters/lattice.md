# Lattice

The optional `lattice` adapter publishes each track as an entity with `client.entities.publish_entity` of the Lattice SDK for Python (`anduril-lattice-sdk`, imported as `anduril`). It is the separate package [`vendors/lattice`](../../vendors/lattice/). The SDK is not a dependency of that package and no file of it is in this repository: the user installs it, if they accept its licence agreement.

| Part | Status |
|---|---|
| entity from a track | built; measured in `vendors/lattice/tests/test_lattice.py` without the SDK |
| request the SDK is asked to send (method, path, headers, exact JSON body) | measured with the SDK installed and its HTTP transport replaced by a local stub; nothing leaves the machine |
| publishing to a Lattice environment | not run |

What Lattice itself requires of an entity is taken from the vendor's public documentation ([Entities API](https://developer.anduril.com/reference/rest/entities/publish-entity)), not from its answers.

## Entity

For the weather-station Position record ([worked example](../mapping.md#worked-example)), published at 2026-01-01T00:00:00Z, the SDK sends `PUT https://<LATTICE_ENDPOINT>/api/v1/entities` with this body (the test asserts it):

```json
{"entityId": "weather-station/WX/KX7", "isLive": true, "expiryTime": "2026-01-01T00:10:00Z",
 "aliases": {"name": "KX7"},
 "location": {"position": {"latitudeDegrees": 64.8378, "longitudeDegrees": -147.7164, "altitudeHaeMeters": 136.5}},
 "milView": {"disposition": "DISPOSITION_FRIENDLY", "environment": "ENVIRONMENT_LAND"},
 "ontology": {"template": "TEMPLATE_ASSET", "platformType": "Weather station"},
 "provenance": {"integrationName": "open-entity-gateway", "dataType": "weather-station/WX/Position",
                "sourceId": "KX7", "sourceUpdateTime": "2025-06-15T15:06:43Z"},
 "indicators": {"simulated": true}}
```

| Entity component | From |
|---|---|
| `entityId` | `lattice.entity_id`, or the track uid |
| `milView.disposition` | the CoT type's affiliation: friend `DISPOSITION_FRIENDLY`, assumed friend `…_ASSUMED_FRIENDLY`, suspect and joker `…_SUSPICIOUS`, hostile and faker `…_HOSTILE`, neutral, pending, unknown; joker and faker also set `indicators.exercise` |
| `milView.environment` | the CoT type's dimension: air, ground `…_LAND`, sea surface `…_SURFACE`, subsurface, space, otherwise `…_UNKNOWN` |
| `location.position` | latitude, longitude, altitude above the ellipsoid |
| `expiryTime` | the moment of publishing plus `stale_minutes` (Lattice takes under 30 days) |
| `provenance.sourceUpdateTime` | the track's time; Lattice applies an update only when it is later than the one it holds |
| `ontology`, `description`, `indicators`, `provenance` | the `lattice` block; defaults `TEMPLATE_TRACK`, integration name `open-entity-gateway`, data type `dictionary/subsystem/message_type`, source id the uid |

No security classification is set on an entity; the block has no key for one.

## Destination and credentials

```yaml
  - name: lattice
    adapter: lattice
```

The credentials are read from the environment under the names the vendor's documentation uses, and never from the configuration: `LATTICE_ENDPOINT` (host name only), `ENVIRONMENT_TOKEN`, and for a Sandboxes environment `SANDBOXES_TOKEN`. An error the gateway logs names the HTTP status or the kind of transport failure, never its text.

## The SDK and its licence

The SDK belongs to Anduril Industries, Inc. and is licensed under the Anduril Lattice Software Development Kit License Agreement (updated July 31, 2026), not under an open-source licence: <https://github.com/anduril/lattice-sdk-python/blob/master/LICENSE>. Read it before installing.

**Installing or using the SDK accepts that agreement** (clause 2.1). Accepting on behalf of an employer states that you have the authority to bind that employer. The agreement includes an indemnity (12.1), usage telemetry (6.1), export restrictions (14.5) and Delaware law with arbitration (14.7); if you act for an organisation, ask its counsel first.

The clauses that shape this package, in summary; the agreement's own words govern:

| Clause | Says | Here |
|---|---|---|
| 3.1 | use the SDK "solely to develop applications for compatible implementations of Lattice" | this adapter is such an application and does nothing else with the SDK |
| 3.2 | do not develop another SDK | the adapter calls the SDK; it is not an SDK |
| 3.4 | no copying, modifying or redistributing any part of the SDK; using it to develop or integrate an application is not a derivative work | no SDK file is here; it is not a dependency; the adapter imports it at run time from the user's installation |
| 3.7 | no right to the vendor's names, trademarks or logos | the names appear only to say which interface the adapter speaks |

## Running the tests with the SDK

On a machine whose user has accepted the agreement:

```bash
pip install anduril-lattice-sdk
pip install --no-deps -e . -e vendors/lattice
python -m pytest vendors/lattice/tests
```

Without the SDK the four entity tests run and the five publishing tests are skipped. CI does not install the SDK.

## Not in a release archive

The SDK's licence does not allow it to be redistributed, so no release archive carries this bridge
([RELEASING.md](../../RELEASING.md)). Install the bridge and the SDK by hand from a checkout, after accepting the SDK's
licence, as above.
