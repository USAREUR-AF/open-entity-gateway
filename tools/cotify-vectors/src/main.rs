// Prints the test vectors in tests/vectors/cotify.jsonl: one JSON line per
// case with the mapping, the record, the creation time and the event that
// first-bite's cotify writes for them. With first-bite checked out next to
// this repository:
//
//     cargo run -q --manifest-path tools/cotify-vectors/Cargo.toml > tests/vectors/cotify.jsonl
use chrono::{TimeZone, Utc};
use serde_json::{json, Value};

fn main() {
    let ws_record: Value = serde_json::from_str(
        // The Position record: line 4 of first-bite's examples/weather-station.records.jsonl.
        include_str!("../../../examples/records/weather-station.records.jsonl").lines().nth(3).unwrap(),
    )
    .unwrap();
    let cases: Vec<(&str, Value, Value, (i64, u32))> = vec![
        ("weather-station position, whole second",
         json!({"uid": ["station_id"], "type": {"value": "a-f-G"}, "position": {"geodetic": ["latitude", "longitude", "altitude"]}, "stale_minutes": 10}),
         ws_record.clone(), (1772625600, 0)),
        ("weather-station position, nanoseconds",
         json!({"uid": ["station_id"], "type": {"value": "a-f-G"}, "position": {"geodetic": ["latitude", "longitude", "altitude"]}, "stale_minutes": 10}),
         ws_record.clone(), (1779882971, 709165450)),
        ("milliseconds and microseconds, default stale",
         json!({"uid": ["station_id", "message_type"], "type": {"value": "a-n-G"}, "position": {"geodetic": ["latitude", "longitude", "altitude"]}}),
         ws_record.clone(), (1779882971, 709000000)),
        ("microseconds",
         json!({"uid": ["station_id"], "type": {"value": "a-n-G"}, "position": {"geodetic": ["latitude", "longitude", "altitude"]}, "stale_minutes": 0}),
         ws_record.clone(), (1779882971, 709165000)),
        ("type lookup, listed value",
         json!({"uid": ["sensor", "contact"], "type": {"field": "affiliation", "values": {"Blue": "a-f-A", "Amber": "a-p-A"}, "default": "a-u-A"}, "position": {"ecef": ["ecef_x", "ecef_y", "ecef_z"]}}),
         json!({"message": {"sensor": "mast1", "contact": "c1", "affiliation": "Blue", "ecef_x": 4201133.037, "ecef_y": 168331.004, "ecef_z": 4780438.968}}), (1772625600, 0)),
        ("type lookup, default, version 1 text values",
         json!({"uid": ["sensor", "contact"], "type": {"field": "affiliation", "values": {"Blue": "a-f-A"}, "default": "a-u-A"}, "position": {"ecef": ["ecef_x", "ecef_y", "ecef_z"]}}),
         json!({"message": {"sensor": "mast1", "contact": "7", "affiliation": "Green", "ecef_x": "4201133.037", "ecef_y": "168331.004", "ecef_z": "4780438.968"}}), (1772625600, 0)),
        ("ecef on the polar axis",
         json!({"uid": ["sensor"], "type": {"value": "a-u-A"}, "position": {"ecef": ["x", "y", "z"]}}),
         json!({"message": {"sensor": "pole", "x": 0.0, "y": 0.0, "z": 6356752.314245}}), (1772625600, 0)),
        ("numbers: whole floats, tiny and large values, integer uid, boolean lookup",
         json!({"uid": ["id", "flag"], "type": {"field": "flag", "values": {"true": "a-h-G"}, "default": "a-u-G"}, "position": {"geodetic": ["lat", "lon", "alt"]}, "stale_minutes": 1}),
         json!({"message": {"id": 7, "flag": true, "lat": 0.0000001, "lon": -180.0, "alt": 100.0}}), (1772625600, 0)),
        ("escaping in uid and type",
         json!({"uid": ["name"], "type": {"value": "a-f-G-'\"<&>"}, "position": {"geodetic": ["lat", "lon", "alt"]}}),
         json!({"message": {"name": "a<b&\"c'd>", "lat": -33.35, "lon": 44.383333, "alt": -12.5}}), (1772625600, 0)),
    ];
    for (name, mapping_json, record, (secs, nanos)) in cases {
        let mapping: cotify::Mapping = serde_json::from_value(mapping_json.clone()).unwrap();
        let now = Utc.timestamp_opt(secs, nanos).unwrap();
        let out = cotify::record_to_cot_at(&mapping, &record.to_string(), now).unwrap();
        let out: Value = serde_json::from_str(&out).unwrap();
        println!("{}", json!({"case": name, "mapping": mapping_json, "record": record, "now": [secs, nanos], "event": out["cot_xml_string"]}));
    }
}
