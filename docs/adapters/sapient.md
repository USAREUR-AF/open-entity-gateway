# SAPIENT (BSI Flex 335 v2.0)

The `sapient` adapter makes the gateway a SAPIENT edge node. It connects to a fusion node or to middleware, registers, reports its status, and sends each track as a detection report. Messages are the protobuf messages of Dstl's proto files for BSI Flex 335 v2.0, each preceded by its length as four little-endian bytes, the framing Dstl's Apex middleware reads.

| Part | Status |
|---|---|
| registration, status reports, detection reports, with every field the proto files mark mandatory | built; measured in `tests/test_sapient.py` against the protos' `is_mandatory` options |
| session against a stand-in fusion node: registration, acknowledgement, status, detections, goodbye; a refused registration sends no detection | measured (`tests/test_sapient.py`) |
| session against Dstl's Apex 4.2.0 with its default validation: registration accepted, no error reported, detections forwarded to a connected decision-making module | measured (`tests/e2e/test_apex.py`) |
| tasks, alerts, range-bearing locations | not built |

## The session

1. Connect to the destination's address.
2. Send a Registration: node type, ICD version `BSI Flex 335 v2.0`, one mode (`default`), detections as latitude/longitude in degrees and metres on the WGS 84 ellipsoid, every classification the mappings can give, ENU velocity when a mapping has `motion`.
3. Wait for the RegistrationAck (default 10 s). Refused or absent: nothing is sent, the payload counts as failed, and the next payload reconnects.
4. Send a StatusReport, `INFO_NEW`, then one every `status_interval_seconds` (`INFO_UNCHANGED`).
5. Send each track as a DetectionReport, at least `detection_spacing_ms` apart (default 100; Apex's default validation refuses detections closer than 80 ms).
6. On close, send a StatusReport `SYSTEM_GOODBYE` (`INFO_NEW`, since the state changed).

Every message's timestamp is the moment it is sent, as the standard defines it, and Apex refuses one more than 0.9 s from its own clock. The track's own time goes in the detection's `track_info` as `capture_time`, which the registration declares.

## Detection report

For the weather-station Position record ([worked example](../mapping.md#worked-example)), as `open-entity-gateway print --adapter sapient` writes it:

```json
{"timestamp": "2026-11-15T20:40:13.909059Z", "node_id": "00000000-0000-4000-8000-000000000000",
 "detection_report": {"report_id": "01M7GF0STNEEZQWFCTNRGY9EAD", "object_id": "1JVTC07CV1S6787NH9QQHHGKVH",
   "location": {"x": -147.7164, "y": 64.8378, "z": 136.5,
                "coordinate_system": "LOCATION_COORDINATE_SYSTEM_LAT_LNG_DEG_M", "datum": "LOCATION_DATUM_WGS84_E"},
   "track_info": [{"type": "capture_time", "value": "2025-06-15T15:06:43Z"}, {"type": "cot_type", "value": "a-f-G"}],
   "id": "KX7"}}
```

| Field | From |
|---|---|
| `object_id` | SHA-256 of the node id and the track uid, written as a ULID: one object keeps its id across restarts; its time part carries no time |
| `report_id` | a new ULID per message |
| `location` | x longitude, y latitude, z altitude when mapped |
| `id` | the mapping's `name` |
| `classification` | the mapping's `sapient.classification`, a constant or a lookup, so the registration can list every class |
| `enu_velocity` | east and north rates from `motion` |

## Destination

```yaml
  - name: sapient-fusion
    adapter: sapient
    address: "198.51.100.7:5020"
    node_id: "7c6f3a52-1e0b-4f7d-8a39-5b2e9d4c1f60"   # this node's UUID v4, chosen once and kept
    node_type: OTHER          # or RADAR, CAMERA, ACOUSTIC, PASSIVE_RF, FUSION_NODE, ...
    node_name: open-entity-gateway
    status_interval_seconds: 10
```

`tls` takes the same keys as the CoT adapter's.

## Mapping block

```yaml
sapient:
  classification: { field: kind, values: { "1": "Vehicle", "2": "Human" }, default: "Other" }
```

## Measured against Apex

`tests/e2e/apex/Dockerfile` builds Apex from `dstl/Apex-SAPIENT-Middleware` at commit `0c8591ae` (4.2.0). `tests/e2e/apex/apex_config.json` opens a Child port (5020) and a Peer port (5001), both BSI Flex 335 v2.0 protobuf, with Apex's own default validation options. The gateway registers as a child; a stand-in decision-making module connects to the Peer port. Apex answers any message that fails its validation with an Error message, and the test fails on any. Received by the module from the gateway's node: registration, status report, three detection reports, status reports.

## The proto files

`tools/fetch-sapient-proto.py` fetches the BSI Flex 335 v2.0 proto files and `proto_options.proto` from `dstl/SAPIENT-Proto-Files` at commit `244a741c`, checks each against its SHA-256, rewrites their `import "sapient_msg/…"` lines to the path inside this package, and generates the Python bindings with `grpcio-tools`. Messages and fields are unchanged. The files are Crown copyright under the Apache License 2.0; the generated directory carries their LICENCE.txt. The standard's text is BSI's and is [published by BSI](https://knowledge.bsigroup.com/products/bsi-flex-335-v2-0-2023-sapient-network-of-autonomous-sensors-and-effectors-interface-control-document-specification-specification).
