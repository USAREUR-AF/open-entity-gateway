# Vendor interfaces

An optional vendor adapter is a separate package that adds an entry point to the group `open_entity_gateway.adapters` and subclasses `open_entity_gateway.adapter.Adapter`. It holds no vendor code, no generated bindings, no credentials and no copied documentation; the user installs the vendor's software under the vendor's own terms. CI never installs a vendor SDK.

Two are built: [Lattice](adapters/lattice.md) and [Picogrid ECN](adapters/picogrid-ecn.md). The interfaces below are listed with what their owners publish and what an adapter would need. Facts are from the cited sources, read 2026-11-15.

## Aaronia AG: AARTOS drone-tracking JSON (Germany)

**Published:** a PDF describing the JSON the AARTOS system writes for antennas, detections and positions ([format description](https://v6-forum.aaronia.de/wp-content/uploads/asgarosforum/30/Drone-Tracking-JSON-Format-10.pdf)). No licence or terms are stated. **Status:** not built. The document describes the system's output, so it is a source of tracks rather than a destination for them; an adapter would need the vendor's statement of an input interface.

## Auterion: Auterion SDK (US, Switzerland)

**Published:** documentation for an SDK for applications on Auterion vehicles, ROS 2 based, with vehicle control, telemetry and a visual-tracking API ([Auterion SDK](https://docs.auterion.com/app-development/auterion-sdk)). No licence found. **Status:** not built. The SDK targets applications running on the vehicle, not a track feed to it.

## BAE Systems: INTeACT SDK (UK)

**Published:** a product page ([INTeACT SDK](https://www.baesystems.com/en-uk/product/inteact-software-development-kit)); the page refused automated reading, so what it states is unconfirmed. No licence or terms found. **Status:** not built. An adapter needs a published interface description and the terms under which a client may be written.

## Palantir Technologies: Foundry, Gotham and Ontology SDKs (US)

**Published:** Apache-2.0 platform SDKs ([foundry-platform-python](https://github.com/palantir/foundry-platform-python), [gotham-platform-python](https://github.com/palantir/gotham-platform-python)) and the Apache-2.0 TypeScript OSDK client ([osdk-ts](https://github.com/palantir/osdk-ts)). An Ontology SDK is generated per customer ontology in that customer's Developer Console ([OSDK overview](https://www.palantir.com/docs/foundry/ontology-sdk/overview/)). The Defense OSDK documentation describes interface types including a tracked entity and a geotemporal observation ([Defense OSDK](https://palantir.com/docs/defense-osdk/api/missionPlanning/overview/about/)).

**Status:** designed, not built. The adapter takes the customer's generated OSDK package as an installed dependency, the user's own, never this project's. The mapping block names the object or interface type and a property for each track value (identifier, position as GeoJSON point, time, affiliation). The adapter writes through the OSDK's create-or-update action for that type. Tests need a stand-in for the generated package, since no public generated package exists.

## Picogrid: Legion REST API (US)

**Published:** documentation at [docs.picogrid.com/legion](https://docs.picogrid.com/legion/) and an OpenAPI 3.1 document (version 3.21.1) downloadable without login. Entities: `POST /v3/entities`, `PUT /v3/entities/{entityId}` (required `name`, `category`, `type`, `status`); positions: `POST /v3/entities/{entityId}/locations` with a GeoJSON-style `position` and a `crs` that defaults to EPSG:4978 (ECEF). Authentication: OAuth 2.0 (authorization code, refresh token, client credentials declared) or a bearer JWT; organisation-scoped calls need `X-ORG-ID`.

**Terms:** the OpenAPI document states no licence and no terms of use. The only legal text found is Picogrid's [commercial licence terms](https://picogrid.com/legal/commercial-license-terms.pdf) for customers under an order form. Whether a client or a mock may be generated from the OpenAPI document, and whether the document may be redistributed, is not addressed in what was read.

**Status:** designed, not built, until those terms are confirmed with Picogrid. The adapter would create an entity once per track uid, keep the server-assigned id, and post each position with `crs: EPSG:4979` (latitude, longitude, height above the ellipsoid). It would be tested against a mock generated locally from the OpenAPI document, which would not be committed.

## Rheinmetall: TacticalAPI (Germany)

**Published:** gRPC protos under EPL-2.0 ([Rheinmetall/tacticalapi](https://github.com/Rheinmetall/tacticalapi), package `rheinmetall.tactical_api.v0`), with services `Situation` (`AddOrUpdateSituationObjects`, …), `BlueForceTracking` (`AddOrUpdateBlueForces`, …) and `OwnPose`, and a C# test client. The repository includes no server or mock; the README gives the ports of one implementing application. No trademark or naming rights are granted.

**Status:** designed, not built. The test client is a client: it cannot receive what a client adapter sends, so a test would run against a server written here from the protos, which tests the adapter against its authors' reading of the protos only. The adapter would generate bindings from the protos at a pinned commit (as the SAPIENT adapter does, EPL-2.0 recorded) and send each track as a `SituationObject` with a `Symbol` through `AddOrUpdateSituationObjects`, the CoT type giving the symbol's identity.

## Skydio: Cloud API (US)

**Published:** REST documentation with an OpenAPI document ([Skydio API docs](https://apidocs.skydio.com/reference/introduction)) for fleet, vehicle and telemetry data. The [API terms](https://www.skydio.com/legal/api-terms-of-service) permit internal business use and require Skydio's written approval for commercial distribution. **Status:** not built. The API exposes the vendor's own fleet data rather than taking tracks from other sources.
