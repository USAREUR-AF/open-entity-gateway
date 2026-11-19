# Track egress: how tracks leave the fabric

**Status: designed; steps 1 to 4 built, step 5 built.** The gateway accepts Canonical Track v2 (step 1), reads the fabric on named key expressions (step 2) and publishes encoded payloads to a fabric key for a pod to deliver (step 4); goat-transformers holds the gateway's example tracks as conformance vectors (step 3). The other steps are not built. Each step is its own pull request, and its tracking issue is linked at the end.

Five goat repositories turn data into tracks or move tracks between systems. This design gives each one a single job: producers publish tracks on the goat fabric (Zenoh); this gateway reads them, maps them and encodes each one once, through goat-transformers; and octo-pod's egress, which is built, carries the encoded payloads to receivers that cannot join the mesh.

<p align="center"><img src="../img/track-egress.svg" width="820" alt="Producers publish tracks or records on the fabric; the gateway reads named keys and encodes each track once, through goat-transformers; it sends to receivers it can reach, or publishes the encoded payload to a release key; an octo-pod egress destination delivers that key to a receiver that cannot join the mesh."></p>

## Who does what

| Repository | Job in this design | What exists today |
|---|---|---|
| [first-bite](https://github.com/USAREUR-AF/first-bite) | Producer: decoded records on the fabric through its Zenoh sink | Built. Also writes CoT to TAK itself, which stays for the CoT-only case |
| [open-billie](https://github.com/USAREUR-AF/open-billie) | Producer that encodes at the node: CoT, ASTERIX CAT062 and SAPIENT on the fabric | Built. Its translator encodes natively or, as a built option off by default, through goat-transformers from Canonical Track v2 ([GOAT_TRANSFORMERS_ENCODER_OPTION.md](https://github.com/USAREUR-AF/open-billie/blob/main/docs/architecture/GOAT_TRANSFORMERS_ENCODER_OPTION.md)). Its encoded keys can go to an octo-pod egress destination without the gateway |
| [octo-pod](https://github.com/USAREUR-AF/octo-pod) | Delivery to receivers that cannot join: `udp://`, `tcp://`, `tcp-server://` and `sapient://` egress destinations, each fed by the pod's downlink allow list | Built (0.4.0 and later). By its own design it converts no format: "the producer supplies the receiver's format before publishing it" ([design-egress-gateway.md](https://github.com/USAREUR-AF/octo-pod/blob/main/docs/design-egress-gateway.md)) |
| [goat-transformers](https://github.com/USAREUR-AF/goat-transformers) | Encoding: one pack per format, seven engines that agree byte for byte | Built. Packs `cot-xml`, `sapient`, `asterix-cat062`, `canonical-track-v2` |
| open-entity-gateway (this repository) | Mapping and encoding: records and tracks in, each format written once, to a destination it can reach or to a release key on the fabric | Inputs and the fabric destination built; encoders are its own today |

## The design

1. **Producers that encode keep encoding; the others publish records or tracks.** open-billie already writes the standard formats at the node, through goat-transformers as an option, and its keys go to a pod as they are. A producer that does not encode publishes Canonical Track v2 (`billie.dev/track-v2`) or records, as first-bite does, and the gateway encodes for it.
2. **The gateway reads the fabric on named keys only.** A key made only of wildcards is refused. What the gateway may read is decided by the fabric's access control.
3. **Each format is encoded once, in goat-transformers.** The gateway's CoT, SAPIENT and ASTERIX CAT062 output comes from the packs through the Python engine, with the context open-billie's option gives them: a scoped track identity (`track.scoped_id`), the stale time and `how` for CoT, SAC and SIC for CAT062, the node and report identifiers for SAPIENT. The gateway keeps per-destination configuration, transport and delivery, the SAPIENT session for destinations it reaches itself, and the vendor adapters.
4. **Two ways out.** A receiver the gateway host can reach gets one of the gateway's own destinations. A receiver that cannot join the mesh is fed by octo-pod: the gateway publishes the encoded payload to a release key (`release/<lane>/…`), and a pod destination whose downlink allow list names that key delivers it. For SAPIENT the gateway publishes detection reports and the pod's `sapient://` destination runs the session. The pod's two decisions (the release flag and the downlink allow list) and its audit stay where they are.
5. **The gateway's rules do not change.** It sends only to destinations a configuration lists, and a track that cannot fill a destination's interface is refused by field name, with nothing sent.

## Order of work

| Step | Repository | Change | Status |
|---|---|---|---|
| 1 | open-entity-gateway | Accept Canonical Track v2 as input | built |
| 2 | open-entity-gateway | Zenoh input on named key expressions | built |
| 3 | goat-transformers | The gateway's example tracks as conformance vectors | built; the differences found are listed in goat-transformers#16 |
| 4 | open-entity-gateway | A fabric destination: publish each encoded payload to a named key, for a pod to deliver | built ([adapters/fabric.md](../adapters/fabric.md)); measured through octo-pod's downlink bridge and egress arm for CoT and CAT062 over `udp://` (`tests/e2e/octo-pod-egress/`) |
| 5 | open-entity-gateway | Encode through goat-transformers with the same context keys open-billie's option uses, as an option off by default with a recorded baseline of differences, as open-billie does | built for CoT, CAT062 and SAPIENT ([goat-transformers-encoder.md](../goat-transformers-encoder.md)) |
| 6 | octo-pod | A documented path: gateway publishes to a release key, a pod destination delivers it | written: octo-pod#35, in review |
| 7 | first-bite, open-billie | Point receivers off the fabric to the right path: first-bite's to the gateway, open-billie's encoded keys to a pod | first-bite: first-bite#74; open-billie: its maintainers |

## Open questions

- Whether first-bite also publishes Canonical Track v2 for dictionaries that carry positions, or leaves record-to-track to gateway mapping files.
- The release lane the gateway publishes encoded payloads under, and who owns it, per goatnet.

## Tracking

| Step | Issue |
|---|---|
| 1 | [open-entity-gateway#6](https://github.com/USAREUR-AF/open-entity-gateway/issues/6) (closed) |
| 2 | [open-entity-gateway#7](https://github.com/USAREUR-AF/open-entity-gateway/issues/7) (closed) |
| 3 | [goat-transformers#14](https://github.com/USAREUR-AF/goat-transformers/issues/14) (closed) |
| 4 | [open-entity-gateway#15](https://github.com/USAREUR-AF/open-entity-gateway/issues/15) |
| 5 | [open-entity-gateway#8](https://github.com/USAREUR-AF/open-entity-gateway/issues/8) |
| 6 | [octo-pod#34](https://github.com/USAREUR-AF/octo-pod/issues/34) |
| 7 | [open-billie#29](https://github.com/USAREUR-AF/open-billie/issues/29), [first-bite#73](https://github.com/USAREUR-AF/first-bite/issues/73) |

OARIS and C2SIM as goat-transformers packs: [goat-transformers#15](https://github.com/USAREUR-AF/goat-transformers/issues/15). A course computed with `atan2` differs in its last digit between engines: [goat-transformers#17](https://github.com/USAREUR-AF/goat-transformers/issues/17).
