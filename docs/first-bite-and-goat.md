# first-bite and goat

[first-bite](https://github.com/USAREUR-AF/first-bite) passively decodes binary messages against a dictionary and publishes one record per message ([docs/record.md](https://github.com/USAREUR-AF/first-bite/blob/main/docs/record.md)). This gateway is a consumer of those records. It holds no first-bite code: it reads the record as first-bite documents it, through first-bite's `rest` sink (one POST per record, JSON or CBOR), a JSON-lines file of records, or first-bite's ZeroMQ publisher socket.

first-bite writes Cursor on Target itself (its `cot` transform and `udp`/`tcp` sinks, [docs/cot-to-tak.md](https://github.com/USAREUR-AF/first-bite/blob/main/docs/cot-to-tak.md)), and through goat-transformers (its `xf` transform). For an open-standard receiver, use those, or first-bite's Zenoh sink and goat-transformers' transport adapters. Use this repository when first-bite's data goes to a vendor system through one of its vendor bridges ([README](../README.md)).

first-bite's example records ([examples/weather-station.records.jsonl](https://github.com/USAREUR-AF/first-bite/blob/main/examples/weather-station.records.jsonl)) are this repository's example and test data, copied unchanged.

Both are part of [goat](https://github.com/USAREUR-AF/goatcore), the open-source data fabric, and follow its contribution and governance rules.
