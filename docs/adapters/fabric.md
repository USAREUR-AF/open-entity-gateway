# Fabric destination (`adapter: fabric`)

Publishes each track, encoded by one of the open-standard adapters, to one key on the goat fabric (Zenoh): one payload per sample. This is the form an [octo-pod](https://github.com/USAREUR-AF/octo-pod) egress destination relays to a receiver that cannot join the mesh. octo-pod converts no format, so the gateway encodes and the pod delivers ([design/track-egress.md](../design/track-egress.md)).

| Status | |
|---|---|
| encoding through `cot`, `asterix-cat062` or `sapient`, and publishing to a key | built |
| a payload published by the gateway received by a Zenoh subscriber on that key, equal to what the encoding adapter writes | measured (`tests/test_fabric.py`, two in-process Zenoh peers) |
| delivered by an octo-pod egress destination: CoT and CAT062 published by the gateway reach a UDP receiver through octo-pod's `downlink-bridge` and `egress-arm`, byte for byte and in order, over mutual TLS; a key the downlink allow list does not name delivers nothing | measured (`tests/e2e/octo-pod-egress/run.sh <octo-pod checkout>`, run by hand: one host, one in-process Zenoh router standing in for the fabric and the pen; octo-pod `fe48297`) |
| delivered through a pod's `sapient://` destination: octo-pod's SAPIENT arm registers with a controller and delivers each detection report the gateway published, in order and unchanged, under the arm's own node id | measured (the same script) |

## Configuration

```yaml
destinations:
  - name: tracks-to-pod
    adapter: fabric
    key: release/lane-a/tracks/cot      # one literal key; a wildcard is refused
    zenoh_config: fabric.json5          # endpoints, TLS, credentials
    encode:
      adapter: cot                      # cot, asterix-cat062 or sapient
      # and that adapter's own options, for example sac and sic, or node_id
```

| Key | Meaning |
|---|---|
| `key` | the key every payload is published on. Normally a release key, which a pod's downlink allow list names. Whether the gateway may publish there is decided by the fabric's access control |
| `zenoh_config`, `connect` | a Zenoh configuration file, and endpoints to add to it; Zenoh 1.9.0 (`pip install 'open-entity-gateway[zenoh]'`) |
| `encode.adapter` | the adapter that writes the payload; the mapping file's block for that adapter applies |
| `encode.*` | that adapter's options, without `address` or `print` |

## Payloads

| `encode.adapter` | One sample holds | octo-pod destination |
|---|---|---|
| `cot` | one CoT event, as the CoT adapter writes it | `udp://` or `tcp://` |
| `asterix-cat062` | one ASTERIX data block | `udp://` |
| `sapient` | one `SapientMessage` carrying a DetectionReport, without the TCP length prefix | `sapient://`, which registers, re-addresses each message under its own node id and frames it |
