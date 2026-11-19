# Encoding through goat-transformers

**Status: built, off by default.** A CoT, ASTERIX CAT062 or SAPIENT destination with `encoder: goat-transformers` writes its payload with the [goat-transformers](https://github.com/USAREUR-AF/goat-transformers) packs instead of the gateway's own encoder. The native encoder stays the default, as open-billie keeps its native encoder beside the same option ([GOAT_TRANSFORMERS_ENCODER_OPTION.md](https://github.com/USAREUR-AF/open-billie/blob/main/docs/architecture/GOAT_TRANSFORMERS_ENCODER_OPTION.md)).

```yaml
destinations:
  - name: tak
    adapter: cot
    address: tak.example.org:8087
    encoder: goat-transformers     # native (the default) or goat-transformers
```

## How it works

The track is written as a Canonical Track v2 and converted by the copied Python engine (`src/open_entity_gateway/_goat_transformers/`, the commit in `COMMIT`). What a track does not carry, or what the gateway decides, goes in as conversion context, with open-billie's keys:

| Format | Value | Context key |
|---|---|---|
| CoT | the track identity (the mapping's `uid`) | `track.scoped_id` |
| CoT | the CoT type (the mapping's `type`) | `type` |
| CoT | stale time, `how` | `stale_ms`, `how` |
| CoT | the callsign (the mapping's `name`) | `callsign`, written as `detail/contact/@callsign` |
| CAT062 | SAC, SIC (the destination's) | `cat062.sac`, `cat062.sic` |
| CAT062 | track number (the block's, or in order seen) | `cat062.track_number` |
| CAT062 | confirmed | `track.confirmed` = 1 |
| SAPIENT | the object id (the gateway's ULID of node id and uid) | `sapient.detection_report_object_id` |
| SAPIENT | the report `id` (the mapping's `name`) | `sapient.detection_report_id` |
| SAPIENT | the class (the block's `classification`) | `track.class` |

SIM comes from the block's `simulated`, as with the native encoder. For SAPIENT the pack writes a SapientMessage; the adapter keeps its DetectionReport, and the session (registration, status reports, the message timestamp, node id and report id) stays the gateway's.

With `goat-transformers` the packs are loaded when the destination is built, so a pack that does not load stops the gateway at start. A destination key only the native encoder writes stops it at start too, rather than being dropped: `access` and `time_form` on CoT, `service_id` on CAT062.

goat-transformers is not on a package index, and the gateway installs only hash-locked packages, so the engine (standard library only) and the four model modules and four packs it needs are copied. `python tools/sync-goat-transformers.py <checkout>` copies them; `--check` checks them against `SHA256SUMS`, and against the checkout when one is given. The tests run the check.

## What is equal and what differs

`tests/test_goat_transformers_encoder.py` encodes every example record and track (ten inputs, four of which a mapping matches: one weather-station record, one harbour-buoy record and the two example tracks) both ways for CoT, CAT062 and SAPIENT, parses both outputs (SAPIENT: every DetectionReport field the generated bindings read) and compares them field by field. Every field that differs is a row of `tests/vectors/goat-transformers-baseline.json`; the test fails when a row appears, disappears or changes. After a deliberate change, run it with `OEG_COMPARE_UPDATE=1` and give each new row its cause here.

Equal on every input: the `uid`, type, position, altitude, `ce` and `le`, `how`, the stale instant, the callsign, course and speed in CoT; the data source, time of day, position, track number, velocity and altitude in CAT062; the object id, report `id`, location, coordinate system, datum, class and velocity in SAPIENT.

| Format | Field | Inputs | Native | goat-transformers | Cause |
|---|---|---|---|---|---|
| CoT | `@time`, `@start`, `@stale` | 4 | the fraction as the destination's `time_form` writes it (`15:06:43Z`) | milliseconds always (`15:06:43.000Z`) | same instant; the pack writes one form |
| CoT | `@access` | 4 | `Undefined` | not written | the pack has no field for it |
| CAT062 | I062/080 MRH | 3 | 1 when an altitude is sent | 0 | the pack does not set it |
| CAT062 | I062/080 SRC | 1 | the block's `altitude_source` | 0 | the pack takes no altitude source |
| CAT062 | I062/080 second and third extents | 4 | not written | written, all zero | the pack writes the extents through the last one it maps |
| SAPIENT | `track_info` (`capture_time`, `cot_type`) | 4 | the two items the registration declares | not written | `track_info` is a list key, and conversion context takes scalar keys only |

A track with no `name` and no motion would also differ in `detail`: the native encoder writes it empty, the pack not at all. Every example input has a `name`, so the baseline holds no such row.

The test suites of the CoT, CAT062, SAPIENT, Canonical Track v2, gateway and fabric destinations run once per encoder (`each_encoder` in `tests/conftest.py`). On `goat-transformers`, 18 runs fail and are marked `goat_differs`, strict, so a mark fails when its difference goes away. They show three differences the example inputs do not reach, and two of the rows above:

| Format | Difference | Runs | Cause |
|---|---|---|---|
| CoT | the event does not validate against the MITRE base-event schema | 14 | no `access` (the row above) |
| CAT062 | `service_id` refused at start; MRH and SRC 0 | 1 | the pack does not map I062/015; the rows above |
| CAT062 | longitude 180 written as 2^25, not -180 | 1 | the pack does not wrap a longitude |
| CAT062 | an altitude of 50000 m written, not refused | 1 | the pack checks only the 16-bit field, not -1500 to 150000 ft |
| SAPIENT | no `track_info` | 1 | the row above |

A receiver gets the same track identity, position and motion on both paths. From the goat-transformers path it does not get `access`, the altitude source and MRH bits, or the SAPIENT `track_info`.
