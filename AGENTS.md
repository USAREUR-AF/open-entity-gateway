# AGENTS.md

Instructions for AI coding agents, and a short version for people working here. [CONTRIBUTING.md](CONTRIBUTING.md) is the full process.

## What this repository is

open-entity-gateway publishes the records [first-bite](https://github.com/USAREUR-AF/first-bite) decodes as tracks, over Cursor-on-Target, SAPIENT and ASTERIX category 062, and through optional vendor adapters. It holds no first-bite code and no vendor code.

## Rules

1. **No default destination.** The gateway sends only to what a configuration lists. Never add a fallback address.
2. **Refuse, do not guess.** A record that cannot fill its mapping or an adapter's block is refused by field name and nothing is sent anywhere.
3. **Nothing from [docs/handling.md](docs/handling.md)** enters a file or a commit message: no credential, no real address, no vendor software, no copyrighted standard text, no controlled message format.
4. **Vendor adapters stay separate.** A vendor SDK is never a dependency of the core, never installed by CI, never vendored. Lattice's SDK is never a dependency at all.
5. **Neutral wording.** A company or product is named only to say which published interface an adapter speaks.
6. **Status words exact** (CONTRIBUTING.md). Claim a test against a system only after running it.
7. **Commits:** Conventional Commits, signed off (`git commit -s`) and signed.
