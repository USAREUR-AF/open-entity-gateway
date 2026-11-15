# Security policy

The gateway sends tracks to destinations its owner configures, and holds the connections and credentials those destinations need. Security reports are prioritised over feature work.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub Private Vulnerability Reporting on this repository: *Security* → *Report a vulnerability*. This opens a private advisory visible only to the maintainers.

Include: a description of the issue and its impact; reproduction steps or a proof of concept; the affected versions or commits; whether you have shared it elsewhere.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | 3 working days |
| Triage and severity | 7 working days |
| Fix or mitigation for a confirmed high or critical issue | 30 days, with a coordinated disclosure date agreed with you |
| Public advisory | when the fix is released, crediting you unless you prefer otherwise |

## Scope

In scope: the code in this repository, including the optional adapters under `vendors/`, and the packages built from it. Out of scope: first-bite, the servers and middleware the tests run against (OpenTAKServer, Apex), and vendor SDKs — report those to their maintainers, and tell us too if the gateway's use of them is affected; any deployment you do not own.

## Supported versions

Pre-1.0, the latest tagged release is supported. Fixes land on `main` and are released by tag.
