# first-bite and goat

[first-bite](https://github.com/USAREUR-AF/first-bite) passively decodes binary messages against a dictionary and publishes one record per message ([docs/record.md](https://github.com/USAREUR-AF/first-bite/blob/main/docs/record.md)). This gateway is a consumer of those records. It holds no first-bite code: it reads the record as first-bite documents it, through first-bite's `rest` sink (one POST per record, JSON or CBOR), a JSON-lines file of records, or first-bite's ZeroMQ publisher socket.

first-bite writes Cursor-on-Target itself (its `cot` transform and `udp`/`tcp` sinks, [docs/cot-to-tak.md](https://github.com/USAREUR-AF/first-bite/blob/main/docs/cot-to-tak.md)). The gateway's mapping files take first-bite's `cot:` mapping keys with the same meaning, so one mapping serves both. Use first-bite's own sinks for CoT alone; use the gateway when the same tracks go to other interfaces, or when the destination needs what [docs/adapters/cot.md](adapters/cot.md) adds (`access`, the `Z` time form, a presence event).

first-bite's example records ([examples/weather-station.records.jsonl](https://github.com/USAREUR-AF/first-bite/blob/main/examples/weather-station.records.jsonl)) are this repository's example and test data, copied unchanged.

Both are part of [goat](https://github.com/USAREUR-AF/goatcore), the open-source data fabric, and follow its contribution and governance rules.
