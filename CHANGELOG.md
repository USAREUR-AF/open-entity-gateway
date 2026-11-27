# Changelog

Each released bridge version has a section `## <bridge> <version>`. The release workflow
(`.github/workflows/bridge-release.yml`) publishes that section as the release's notes and refuses a tag whose bridge
and version have none. How a release is cut, checked and installed: [RELEASING.md](RELEASING.md).

## picogrid-ecn 0.1.0

The first release: the Picogrid ECN bridge with the shared core 0.1.0, as one archive per architecture for Ubuntu
24.04's own `python3` (amd64, arm64). Nothing needs the network where it is installed.

- The bridge publishes tracks from the goat fabric as entities to an ECN through Picogrid's SDK,
  `picogrid-ecn-client` 0.2.0. The SDK is under the Mozilla Public License 2.0 and is shipped unmodified in the
  archive, with its licence and where its source is (`licenses/THIRD_PARTY.txt`).
- The shared core is what `main` holds, including its own CoT, SAPIENT and ASTERIX CAT062 senders and encoders, which
  stay until goat-transformers passes the same end-to-end tests (goatcore ADR 1255, item 6).
- `install.sh` installs it under `/opt/vendor-bridges/picogrid-ecn/`, with the service
  `vendor-bridges-picogrid-ecn.service`; the archive's `INSTALL.md` says how to configure it.
- Not in this release: the Lattice bridge. Its SDK may not be redistributed, so no archive carries it. Install that
  bridge and its SDK by hand from a checkout, after accepting the SDK's licence
  ([docs/adapters/lattice.md](docs/adapters/lattice.md)).
