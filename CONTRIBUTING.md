# Contributing

Thank you for working on open-entity-gateway, part of goat. This page is the whole process.

## The short version

1. Fork, or branch from `main` if you have write access. Branch names: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`.
2. Make the change. Run the gates below. Update the page under `docs/` that describes what you changed, in the same change.
3. Commit with a [Conventional Commits](https://www.conventionalcommits.org/) subject and a DCO sign-off: `git commit -s -m "feat(cot): …"`.
4. Open a pull request against `main`. One approving review from a code owner merges it, by squash.

## Commits

- **Conventional Commits.** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, with a scope naming the part (`cot`, `sapient`, `asterix`, `mapping`, `lattice`, `picogrid-ecn`). The subject is 100 characters or fewer and says what changed, in the imperative.
- **DCO sign-off on every commit** (`git commit -s`). There is no CLA. The policy is in [Policy](#policy).
- **Signed commits** are required on `main`.
- One logical change per pull request.

## Gates

CI (`.github/workflows/ci.yml`) runs these; each runs locally the same way:

```bash
pip install --require-hashes -r requirements/runtime.txt -r requirements/dev.txt -r requirements/tools.txt
pip install --no-deps -e . -e vendors/lattice
python tools/fetch-cot-xsd.py && python tools/fetch-sapient-proto.py
ruff check . && ruff format --check .
mypy src vendors/lattice/src
python -m pytest
```

| Change touches | Also run |
|---|---|
| a CoT event | the vectors still match: `python -m pytest tests/test_cot.py`; regenerate them only from first-bite (`tools/cotify-vectors`) |
| a server-facing adapter (CoT, SAPIENT) | `tests/e2e/run-on-build-host.sh` on a host with docker |
| `vendors/picogrid-ecn` | its tests with the SDK installed: [docs/adapters/picogrid-ecn.md](docs/adapters/picogrid-ecn.md) |
| `vendors/lattice` | its tests with the SDK installed, by someone who has accepted its licence: [docs/adapters/lattice.md](docs/adapters/lattice.md) |
| a dependency | edit `requirements/*.in`, then `uv pip compile --generate-hashes` for each `.txt` |
| anything, before publishing | first-bite's leak check, `tools/check-public-tree.sh`, with the maintainers' needle list |

CI's checks never install a vendor SDK. The release archive of a bridge whose SDK may be redistributed carries that SDK
(`packaging/`, [RELEASING.md](RELEASING.md)); its CI job builds the archive apart from the checks.

## Status words

Each page under `docs/` states, per part, its status, and the words mean exactly this:

| Word | Means |
|---|---|
| built | the code exists and its tests pass |
| measured | a command ran against the named target and produced the stated result |
| designed, not built | decided, not implemented |
| not built | nothing exists |

A test against a system is claimed only when it was run against that system.

## Review

Reviewers look for: the change does what its subject says and nothing else; the gates pass; the docs moved with the code; status words are honest; nothing in [docs/handling.md](docs/handling.md) entered the tree. Review comments are about the change, not the author.

## Reporting a bug or proposing a feature

Open an issue. For a vulnerability, **do not open an issue** — follow [SECURITY.md](SECURITY.md).

## Licence

Portions written by U.S. Federal government employees within the scope of their employment are not subject to copyright in the U.S.; in countries where copyright is recognized, they and all other contributions are licensed under the [Apache License 2.0](LICENSE). See [INTENT.md](INTENT.md). Contributions are accepted under the [Policy](#policy) below.

**NOTE: This CONTRIBUTING.md is for software contributions. You do not need to follow the Developer's Certificate of Origin (DCO) process for commenting on this repository's documentation, such as CONTRIBUTING.md, INTENT.md, etc. or for submitting issues.**

## Policy

### 1. Introduction

The project maintainer for this Project will only accept contributions using the Developer's Certificate of Origin 1.1 located at [developercertificate.org](https://developercertificate.org) ("DCO"). The DCO is a legally binding statement asserting that you are the creator of your contribution, or that you otherwise have the authority to distribute the contribution, and that you are intentionally making the contribution available under the license associated with the Project ("License").

### 2. Developer Certificate of Origin Process

Before submitting contributing code to this repository for the first time, you'll need to sign a Developer Certificate of Origin (DCO) (see below). To agree to the DCO, add a `Signed-off-by` line to every commit with `git commit -s`. At a high level, signing off tells us that you have the right to submit the work you're contributing and indicates that you consent to our treating the contribution in a way consistent with the license associated with this software (as described in [LICENSE](LICENSE) and [INTENT.md](INTENT.md)) and its documentation ("Project").

### 3. Important Points

Pseudonymous or anonymous contributions are permissible, but you must be reachable at the email address provided in the Signed-off-by line.

If your contribution is significant, you are also welcome to add your name and copyright date to the source file header.

U.S. Federal law prevents the government from accepting gratuitous services unless certain conditions are met. By submitting a pull request, you acknowledge that your services are offered without expectation of payment and that you expressly waive any future pay claims against the U.S. Federal government related to your contribution.

If you are a U.S. Federal government employee and use a `*.mil` or `*.gov` email address, we interpret your Signed-off-by to mean that the contribution was created in whole or in part by you and that your contribution is not subject to copyright protections.

### 4. DCO Text

The full text of the DCO is included below and is available online at [developercertificate.org](https://developercertificate.org):

```txt
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
1 Letterman Drive
Suite D4700
San Francisco, CA, 94129

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
