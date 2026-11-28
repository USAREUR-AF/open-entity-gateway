# Install a vendor bridge from its release archive

The archive `vendor-bridges-<bridge>_<version>_linux_<arch>.tar.gz` holds one bridge, the shared core (with its
open-standard senders) and every dependency, for Ubuntu 24.04's `/usr/bin/python3` (the version in `PYTHON`) on one
architecture. The host needs no network and no `pip`. Check the release before you unpack it (RELEASING.md in the
source repository).

## Install

```bash
d=$(sudo mktemp -d)
sudo tar -xzf vendor-bridges-<bridge>_<version>_linux_<arch>.tar.gz -C "$d"
sudo "$d"/vendor-bridges-<bridge>_<version>_linux_<arch>/install.sh
```

Unpack it as root: `install.sh` refuses a tree anyone but root owns or can change, because what it copies becomes
code root installs and the service runs.

It puts the tree at `/opt/vendor-bridges/<bridge>/<version>/`, points `current` at it (the version before stays as
`previous`), creates the system user `vendor-bridges`, and installs and enables `vendor-bridges-<bridge>.service`.
It writes no configuration, so the service starts only once its configuration exists.

## Configure

| File | What it holds |
|---|---|
| `/etc/vendor-bridges/<bridge>.yml` | the gateway configuration: its destinations and `mappings:`. Point `mappings:` at `/opt/vendor-bridges/<bridge>/current/share/mappings` or your own; `share/gateway.example.yml` is the example |
| `/etc/vendor-bridges/fabric.json5` | the Zenoh client configuration: endpoints, TLS, credentials |
| `/etc/vendor-bridges/<bridge>.env` | `VENDOR_BRIDGES_KEYS="--key <key expression>"` (one or more `--key`), and any variable the vendor's SDK reads |

Make each one root's and readable by the `vendor-bridges` group, for example
`sudo install -m 0640 -g vendor-bridges gateway.yml /etc/vendor-bridges/<bridge>.yml`. Then:

```bash
sudo systemctl restart vendor-bridges-<bridge>.service
systemctl status vendor-bridges-<bridge>.service
```

The service does not start until all three are in place. It runs `check` before it starts, and then subscribes to the
key expressions in `VENDOR_BRIDGES_KEYS`. A service that keeps failing stops after five starts in five minutes.

## Upgrade and roll back

Run the newer archive's `install.sh`; the service restarts on it. If it does not stay up for ten seconds, `current`
goes back to the version before, that one is restarted, and `install.sh` fails. To roll back by hand, run the earlier
archive's `install.sh`: `current` points back at that version, which is still on the host. Running the same archive's
`install.sh` again changes nothing. A version is installed once: an archive of an installed version with other
contents is refused. Old versions stay under `/opt/vendor-bridges/<bridge>/` until you remove them.

## Licences

`licenses/THIRD_PARTY.txt` names every distribution the archive carries, its licence, its licence files under
`licenses/`, and where its source is.
