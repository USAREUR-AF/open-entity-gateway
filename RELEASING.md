# Releasing a vendor bridge

A release is one bridge: a git tag `<bridge>-v<version>` on `main`, where `<bridge>` is a directory under `vendors/`
and `<version>` is its `pyproject.toml`'s version. The tag starts `.github/workflows/bridge-release.yml`, which builds,
checks, signs and publishes it. Nothing is built or signed on a person's machine, and there is no long-lived signing
key.

Only a bridge listed in `packaging/redistributable.txt` is released: one whose vendor SDK may be redistributed, so its
archive can carry it with its licence. A bridge whose SDK may not be redistributed (Lattice today) is never in an
archive; its users install it and its SDK by hand from a checkout, after accepting the SDK's licence.

## What a release contains

| File | What it is |
|---|---|
| `vendor-bridges-<bridge>_<version>_linux_amd64.tar.gz` | the bridge, the shared core and every dependency for Ubuntu 24.04's `/usr/bin/python3` on x86-64, with its command, service unit, installer, licences and `INSTALL.md` |
| `vendor-bridges-<bridge>_<version>_linux_arm64.tar.gz` | the same for arm64 |
| `SHA256SUMS` | the SHA-256 of both archives |
| `SHA256SUMS.sigstore.json` | the keyless signature of `SHA256SUMS`: a Sigstore bundle v0.3, one `hashedrekord` entry in the public Rekor log, its certificate naming this workflow at the tag |

`packaging/build-archive.sh` builds an archive from the hash-locked requirements files (`requirements/runtime.txt` and
the bridge's `requirements.txt`, wheels only) and the core and bridge built from the commit, with the SAPIENT
bindings. The archive's native wheels are for the Python it was built with; its command refuses another. A build of
one commit gives the same bytes.

Before it publishes, the workflow checks that the tagged commit is on `main`; that the tag names a listed bridge and
its `pyproject.toml` version, and `CHANGELOG.md` has its `## <bridge> <version>` section; that each archive, on Ubuntu
24.04 of its own architecture, runs with an empty environment, loads its bridge and SDK (`check`, connecting to
nothing), names every distribution it carries in `licenses/THIRD_PARTY.txt`, and installs as root with its unit loaded
and not started (`packaging/smoke-test.sh --install`); that the signature verifies for this workflow at the tag; and
that the bundle has the shape goatbuilder reads. It signs with the Sigstore signing config pinned in
`packaging/release/signing-config.json` (Fulcio and the Rekor v1 log) and cosign 3.1.4; a change to either is a pull
request here.

## Cut a release

1. In one pull request: set the bridge's version in `vendors/<bridge>/pyproject.toml` and add its section to
   `CHANGELOG.md`. A new bridge whose SDK may be redistributed is also added to `packaging/redistributable.txt`, with
   its SDK's licence named in the pull request.
2. After it merges, run the workflow by hand on `main`, naming the bridge (Actions, bridge-release, Run workflow).
   That is a dry run: the same build, checks and signature, kept as the run's artifact `signed`, with nothing
   published. Read its file list.
3. Tag the merged commit with a signed tag and push it:
   `git tag -s <bridge>-v<version> <commit> -m "<bridge> <version>"`, then `git push origin <bridge>-v<version>`.
4. The workflow publishes the GitHub Release. A tag is public and is never moved or reused: a fix is a new version.

## Check a release by hand

```
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
  --certificate-identity https://github.com/<owner>/<repository>/.github/workflows/bridge-release.yml@refs/tags/<bridge>-v<version> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sha256sum -c SHA256SUMS
```

## Install a release

By hand: the archive's `INSTALL.md` (unpack, `sudo ./install.sh`, then three configuration files). With goatbuilder:
a release component pinned at `<owner>/<repository>@<bridge>-v<version>@sha256:<SHA-256 of SHA256SUMS>`, its
`release:` naming `checksums: SHA256SUMS`, `bundle: sigstore`, the two archives, and the signer
`https://github.com/<owner>/<repository>/.github/workflows/bridge-release.yml`; goatbuilder verifies the release before
any host is touched, and the play runs the verified archive's own `install.sh`.
