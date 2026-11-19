# Mapping files

A mapping file says which records become tracks and where each value of a track comes from. It is data: one YAML file per message, read when the gateway starts. A file with an unknown key, a missing required key, or a value outside its list stops the gateway with the file and key named.

## Keys

| Key | Required | Meaning |
|---|---|---|
| `match.subsystem`, `match.message_type` | yes | the record members that must equal these (first-bite docs/record.md) |
| `match.dictionary` | no | the record's `dictionary` must also equal this |
| `uid` | yes | one or more message field names; their values joined with `_` are the track's identifier. The same uid updates the same track |
| `type` | yes | the CoT type: `{ value: "a-f-G" }`, or `{ field: f, values: { "A": "a-h-A" }, default: "a-u-A" }`. A lookup gives `default` for a value not listed; a missing field refuses the record |
| `position.geodetic` | one of the two | field names of latitude and longitude in degrees and, optionally, altitude in metres above the WGS 84 ellipsoid |
| `position.ecef` | one of the two | field names of WGS 84 Earth-centred, Earth-fixed x, y, z in metres, converted to geodetic as first-bite converts them |
| `stale_minutes` | no, default 60 | minutes after the track's time at which it goes stale; each adapter writes it as its interface's expiry |
| `form` | no | `event`, the only value accepted |
| `time` | no, default `capture` | `capture`: the record's capture time (`timestamp`); `received`: the moment the gateway received the record |
| `name` | no | a display name, in any value form below |
| `motion.course`, `motion.speed` | no, together | field names of course over ground in degrees from true north and speed in metres per second |
| `motion.east`, `motion.north` | no, together, with course and speed | field names of the velocity's east and north components in metres per second, when the source gives them; SAPIENT writes these as they are instead of computing them from course and speed |
| `optional` | no | the altitude field and the motion fields that may be absent or null: the track then has no altitude, or no motion. Any other field listed is refused. [mappings/canonical-track-v2.yml](../mappings/canonical-track-v2.yml) uses it, because a Canonical Track v2 may carry neither |
| `asterix`, `sapient`, `lattice`, `picogrid_ecn` | no | one block per adapter, read by that adapter only; see [adapters/](adapters/) |

The track's affiliation and battle dimension are read from the CoT type, as its atoms define them: `a-f-G` is friend, ground; `a-h-A` hostile, air. Each adapter writes them in its interface's terms.

### Value forms

`name` and the adapter blocks take these forms:

| Form | Gives |
|---|---|
| `"text"` or `{ value: "text" }` | a constant |
| `{ field: station_id }` | a field of the record's `message` |
| `{ record: subsystem }` | a member of the record: `subsystem`, `message_type`, `dictionary`, `dictionary_version`, `capture_interface`, `application_version`, `data_source_network_name`, `data_source_owner`, `data_source_system_type` |
| `{ field: kind, values: { "A": X }, default: Z }` | a lookup on a field or member; without `default`, a value not listed refuses the record |
| `[ a, b, c ]` | the parts, each in any form, joined with `/` |

A field is read as text: the label where the dictionary gives one, otherwise the number as first-bite writes it. Version 1 records, which carry every value as text, map to the same track as version 2 records.

## Refusal

A record is refused, and no destination receives anything for it, when a field its mapping or any configured adapter's block needs is missing, null, not a number where a number is needed, a latitude outside ±90 or a longitude outside ±180, or a value an adapter's interface cannot carry. Every payload is written before any is sent, so one adapter's refusal stops all of them. The log line names the mapping, the topic and the field:

```text
refused WX/Position (mappings/weather-station.position.yml): message field 'latitude' is missing or null; nothing sent
```

## Relationship to first-bite's `cot:` mapping

The five keys first-bite's cot transform takes (first-bite `cotify/README.md`) are taken here with the same meaning, the same defaults and the same error for each mistake. The CoT event the gateway writes from them is first-bite's, byte for byte, when the destination sets `time_form: first-bite` and the event's `access` attribute is removed: `tests/test_cot.py` checks this against [events first-bite wrote](../tests/vectors/README.md).

The gateway differs where first-bite's mapping does not reach:

| first-bite | gateway | Why |
|---|---|---|
| event time is the moment the event is built | `time: capture` (default) uses the record's capture time; `time: received` is first-bite's behaviour | one record gives the same track time at every destination and on every replay |
| `form: flat` writes an unclosed start tag | refused when the file is loaded | it is not an XML document and cannot be checked against the CoT schema |
| geodetic position needs three fields | two fields are accepted; altitude is then unknown | positions without altitude exist; each adapter writes its interface's unknown value |
| no display name, no motion | `name`, `motion` | the other interfaces carry them |
| no latitude or longitude range check | out of range refuses the record | every interface here has the same range |

## Worked example

The weather-station Position record, line 4 of [examples/records/weather-station.records.jsonl](../examples/records/weather-station.records.jsonl) (first-bite's own example; members the mapping does not read left out):

```json
{"timestamp":{"secs_since_epoch":1750000003,"nanos_since_epoch":0},
 "subsystem":"WX","message_type":"Position",
 "message":{"altitude":136.5,"latitude":64.8378,"longitude":-147.7164,
            "message_length":27,"message_type":"Position","station_id":"KX7"},
 "dictionary":"weather-station","dictionary_version":"1.0.0"}
```

With [mappings/weather-station.position.yml](../mappings/weather-station.position.yml) it becomes this track:

| Track value | From | Value |
|---|---|---|
| uid | `uid: [station_id]` | `KX7` |
| CoT type | `type: { value: "a-f-G" }` | `a-f-G`: friend, ground |
| position | `position.geodetic` | 64.8378, -147.7164, 136.5 m |
| time | `time: capture` | 2025-06-15T15:06:43Z |
| stale | `stale_minutes: 10` | 2025-06-15T15:16:43Z |
| name | `name: { field: station_id }` | `KX7` |

What each adapter sends for it is in that adapter's page.

## Canonical Track v2

A Canonical Track v2 object (`apiVersion: billie.dev/track-v2`, `kind: Track`) is accepted wherever a record is: over HTTP, from a JSON-lines file and from the ZeroMQ socket. The gateway turns it into a record with subsystem `TRACK`, message type `Track` and dictionary `canonical-track-v2`, whose message fields are `track_id`, `source_id`, `generation`, `latitude`, `longitude`, `altitude` when present, `classification` (`unknown` when absent), `course` and `speed` from velocity east and north when present, and `confidence`, `simulated` and `test_data` when present. [mappings/canonical-track-v2.yml](../mappings/canonical-track-v2.yml) maps that record; include it in a configuration's `mappings` to accept tracks. A track missing `trackId`, `sourceId`, `trackGeneration`, `observedAt` or a position is refused, the log names the member, and nothing is sent. Example tracks: [examples/tracks/](../examples/tracks/).
