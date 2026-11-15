# Handling rules

This repository is public. These things are never committed, in a file, a test fixture, an example or a commit message:

| Never | Instead |
|---|---|
| credentials: tokens, passwords, private keys, certificates issued to a real system, SDK profiles | the environment or the vendor SDK's own credential store; tests make throwaway keys at run time |
| an address, host name, port plan or identifier of a real deployment | documentation addresses (RFC 5737: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), `example.org`, made-up UUIDs |
| captured traffic or records from a real link | first-bite's synthetic example records |
| a fielded or controlled message format or dictionary | synthetic dictionaries; controlled data links and message standards are out of scope (README) |
| vendor software: SDK files, generated bindings of a vendor SDK, copied vendor documentation | a dependency the user installs, and a link to the vendor's page |
| the text of a standard under copyright (BSI Flex 335, EUROCONTROL ASTERIX) | a link to the publisher |
| third-party files the tools fetch (SAPIENT protos, MITRE schemas) | `tools/fetch-*.py` at a pinned commit and SHA-256; the output is ignored by git |
| a security classification or caveat chosen on the data owner's behalf | the destination's owner sets CoT `access`; no adapter sets any other marking |

The destinations a gateway sends to are chosen by the owner of the data, in that owner's configuration file, which belongs with the deployment, not here. [examples/gateway.yml](../examples/gateway.yml) uses documentation addresses only.

A new file that matches `*.pem`, `*.key`, `*.p12`, `*.pfx`, `.env`, `*.pcap` or `*.pcapng` is ignored by git (`.gitignore`); the leak check in [CONTRIBUTING.md](../CONTRIBUTING.md) covers the rest.
