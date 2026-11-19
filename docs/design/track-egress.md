# Track egress through this gateway: withdrawn

**Status: withdrawn** by goatcore [ADR 1255](https://github.com/USAREUR-AF/goatcore/pull/1084) (proposed).

This page once proposed that every track leaving the goat fabric pass through this gateway, which would encode it and publish
it to a second key for an octo-pod to relay. That path is withdrawn. Tracks leave the fabric by one of three routes, each one
hop from the fabric:

| Receiver | Route |
|---|---|
| A system that speaks an open standard: a TAK server, a SAPIENT controller, an ASTERIX consumer | [goat-transformers](https://github.com/USAREUR-AF/goat-transformers) and its transport adapters (`adapters/`, `goat-xf-pipe`) |
| A vendor system reached through the vendor's software kit | this repository's vendor bridges |
| A receiver that cannot join the mesh | an [octo-pod](https://github.com/USAREUR-AF/octo-pod)'s release gate and egress, from whatever publishes the data |

The [README](../../README.md) shows what this repository holds and what is moving to goat-transformers.
