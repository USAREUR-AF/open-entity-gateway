# Encoding through goat-transformers

**Status: built, off by default.** A CoT or ASTERIX CAT062 destination with `encoder: goat-transformers` writes its payload with the [goat-transformers](https://github.com/USAREUR-AF/goat-transformers) packs instead of the gateway's own encoder. The native encoder stays the default, as open-billie keeps its native encoder beside the same option ([GOAT_TRANSFORMERS_ENCODER_OPTION.md](https://github.com/USAREUR-AF/open-billie/blob/main/docs/architecture/GOAT_TRANSFORMERS_ENCODER_OPTION.md)). SAPIENT is not built for this option.

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
| CAT062 | SAC, SIC (the destination's) | `cat062.sac`, `cat062.sic` |
| CAT062 | track number (the block's, or in order seen) | `cat062.track_number` |
| CAT062 | confirmed | `track.confirmed` = 1 |

SIM comes from the block's `simulated`, as with the native encoder.

goat-transformers is not on a package index, and the gateway installs only hash-locked packages, so the engine (standard library only) and the four model modules and four packs it needs are copied. `python tools/sync-goat-transformers.py <checkout>` copies them; `--check` checks them against `SHA256SUMS`, and against the checkout when one is given. The tests run the check.

## What is equal and what differs

`tests/test_goat_transformers_encoder.py` encodes every example record and track (ten inputs, four of which a mapping matches: one weather-station record, one harbour-buoy record and the two example tracks) both ways, parses both outputs and compares them field by field. Every field that differs is a row of `tests/vectors/goat-transformers-baseline.json`; the test fails when a row appears, disappears or changes. After a deliberate change, run it with `OEG_COMPARE_UPDATE=1` and give each new row its cause here.

Equal on every input: the `uid`, type, position, altitude, `ce` and `le`, `how`, the stale instant, course and speed in CoT; the data source, time of day, position, track number, velocity and altitude in CAT062.

| Format | Field | Inputs | Native | goat-transformers | Cause |
|---|---|---|---|---|---|
| CoT | `@time`, `@start`, `@stale` | 4 | the fraction as the destination's `time_form` writes it (`15:06:43Z`) | milliseconds always (`15:06:43.000Z`) | same instant; the pack writes one form |
| CoT | `@access` | 4 | `Undefined` | not written | the pack has no field for it |
| CoT | `detail/contact/@callsign` | 4 | the mapping's `name` | not written | the pack does not map a callsign from a track |
| CoT | `detail` | 2 | written, empty when it has no child | not written when empty | |
| CAT062 | I062/080 MRH | 3 | 1 when an altitude is sent | 0 | the pack does not set it |
| CAT062 | I062/080 SRC | 1 | the block's `altitude_source` | 0 | the pack takes no altitude source |
| CAT062 | I062/080 second and third extents | 4 | not written | written, all zero | the pack writes the extents through the last one it maps |

A receiver gets the same track identity, position and motion on both paths. From the goat-transformers path it does not get the callsign, `access`, or the altitude source and MRH bits.
