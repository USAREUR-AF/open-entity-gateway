# Test vectors

`cotify.jsonl`: nine Cursor-on-Target events written by first-bite's `cotify` crate, one JSON line per case with the mapping, the record, the creation time and the event. `tests/test_cot.py` checks that the gateway writes each event byte for byte (with `time: received` and `time_form: first-bite`, before adding `access`).

Made by `tools/cotify-vectors` against first-bite at commit `d65b0389013ca36e14d27b5e802d16e9390f7f5f`, with first-bite checked out next to this repository:

```bash
cargo run -q --manifest-path tools/cotify-vectors/Cargo.toml > tests/vectors/cotify.jsonl
```
