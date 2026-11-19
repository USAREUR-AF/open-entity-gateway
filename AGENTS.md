# AGENTS.md

Instructions for AI coding agents, and a short version for people working here. [CONTRIBUTING.md](CONTRIBUTING.md) is the full process.

## What this repository is

open-entity-gateway holds bridges between the goat fabric and vendor systems, one package per vendor under `vendors/`. Its scope is set by goatcore [ADR 1255](https://github.com/USAREUR-AF/goatcore/blob/main/docs/adr/1255-vendor-systems-connect-through-one-vendor-bridges-repository-and-open-standards-go-through-goat-transformers.md) (accepted). Open standards (Cursor on Target, SAPIENT, ASTERIX) belong to [goat-transformers](https://github.com/USAREUR-AF/goat-transformers); this repository's own senders and encoders for them are moving there and keep working until goat-transformers passes the same tests.

## Rules

1. **No default destination.** The gateway sends only to what a configuration lists. Never add a fallback address.
2. **Refuse, do not guess.** A record that cannot fill its mapping or an adapter's block is refused by field name and nothing is sent anywhere.
3. **Nothing from [docs/handling.md](docs/handling.md)** enters a file or a commit message: no credential, no real address, no vendor software, no copyrighted standard text, no controlled message format.
4. **Vendor adapters stay separate.** A vendor SDK is never a dependency of the core, never installed by CI, never vendored. Lattice's SDK is never a dependency at all.
5. **Neutral wording.** A company or product is named only to say which published interface an adapter speaks.
6. **Status words exact** (CONTRIBUTING.md). Claim a test against a system only after running it.
7. **Scope (ADR 1255).** Add no open-standard sender, encoder or data model here; those go to goat-transformers. Add no relay that republishes another component's output for a third to deliver. A new bridge is a new package under `vendors/`.
8. **Commits:** Conventional Commits, signed off (`git commit -s`) and signed.
