# Picogrid Expeditionary C2 Node

The optional `picogrid-ecn` adapter publishes each track as an entity, with its position, to a Picogrid Expeditionary C2 Node through Picogrid's ECN SDK for Python (`picogrid-ecn-client` 0.2.0, MPL-2.0). It is the separate package [`vendors/picogrid-ecn`](../../vendors/picogrid-ecn/), which depends on the SDK from PyPI; no file of the SDK is in this repository.

| Part | Status |
|---|---|
| entity and position from a track | built |
| publishing through the SDK to the SDK's offline mock (`picogrid_ecn_client.testing.MockECN`); a second SDK client watching the integration receives and decodes both example tracks | measured (`vendors/picogrid-ecn/tests/test_picogrid_ecn.py`) |
| publishing to an ECN: run 2026-11-16 on a Picogrid test unit (edge-mqtt `vjiatf-v31.rc4`, mTLS to the unit's broker on 8883, SDK 0.2.0); a second SDK client watching the integration received and decoded both example tracks with the fields above; the weather station (DEVICE, QoS 1) had a broker acknowledgement and the buoy (TRACK, QoS 0) a completed local send | measured (a one-off run by a throwaway script that is not in this repository) |
| the buoy track in OrionX (Intercept) on the same unit: with its time moved to now, republished every 5 s for 150 s, it appeared in OrionX's `/api/state` within seconds as a `FRIEND` track from `open-entity-gateway-test` at the buoy's position with its velocity, and was seen drawn on the map (blue friendly arrow, label `b55f62`) | measured (`vendors/picogrid-ecn/tests/live_orionx.py`, a script run by hand: `publish` where the broker is reachable, `verify` over https, without checking the unit's own certificate, with `ORIONX_URL`, `ORIONX_USER`, `ORIONX_PASSWORD` in the environment) |

## Entity

For the weather-station Position record ([worked example](../mapping.md#worked-example)), as the watching client decoded it from the mock:

```json
{"id":"29d4256c-9b99-5254-846f-ab16ac53dc7d","category":"DEVICE","integration":"open-entity-gateway-test",
 "recorded_at":"2025-06-15T15:06:43Z","type":"weather-station","name":"KX7","status":"ACTIVE","affiliation":"FRIEND",
 "metadata":{"properties":{}},
 "position":{"latitude":64.8378,"longitude":-147.7164,"recorded_at":"2025-06-15T15:06:43Z","altitude":136.5}}
```

| Entity field | From |
|---|---|
| `id` | UUID version 5 of the integration name and the track uid: the same track keeps its id |
| `category`, `type`, `status` | the mapping's `picogrid_ecn` block; without one: `TRACK`, the CoT type, `ACTIVE` |
| `affiliation` | the CoT type's affiliation: friend `FRIEND`, hostile `HOSTILE`, suspect `SUSPECT`, joker `JOKER`, faker `FAKER`, none `NONE_SPECIFIED`, … |
| `recorded_at`, `position.recorded_at` | the track's time (the SDK requires the two to be equal) |
| `position` | latitude, longitude, altitude above the WGS 84 ellipsoid; `bearing` and `velocity` (north, east, down) from `motion` |

The SDK publishes a TRACK entity at MQTT QoS 0 and other categories at QoS 1 (its wire-format page). In the measured run the weather station (DEVICE) had a broker acknowledgement and the buoy (TRACK) a completed local send. The SDK's entity has no expiry, so `stale_minutes` is not sent.

## Destination

```yaml
  - name: ecn
    adapter: picogrid-ecn
    profile: my-ecn          # an SDK profile (picogrid-ecn configure); or none: the SDK reads its ECN_* variables
    timeout_seconds: 10
```

The SDK owns the connection, TLS and authentication. No credential is in the gateway's configuration.

## Mapping block

```yaml
picogrid_ecn:
  category: DEVICE           # DEVICE, DETECTION, TRACK, SYSTEM, SENSOR, ALERT, WEATHER, GEOMETRIC, ZONE, VEHICLE, UXV
  type: "weather-station"    # any value form, 1 to 128 characters
```

## Running the tests

```bash
python -m venv .venv-ecn && . .venv-ecn/bin/activate
pip install --require-hashes -r requirements/runtime.txt -r requirements/dev.txt -r vendors/picogrid-ecn/requirements.txt
pip install --no-deps -e . -e vendors/picogrid-ecn
python -m pytest vendors/picogrid-ecn/tests
```

CI does not install the SDK.
