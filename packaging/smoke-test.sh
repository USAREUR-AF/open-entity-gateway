#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Check one release archive as a host will run it, with no network and nothing from this
# checkout on the path:
#
#   packaging/smoke-test.sh <archive.tar.gz> [--install]
#
# Unpacks it in a fresh directory; the command must run with an empty environment and load
# the bridge and its SDK (`check` on a configuration naming the bridge, with a stand-in SDK
# configuration that connects to nothing); licences/THIRD_PARTY.txt must name every
# distribution in lib/; install.sh --root must lay out /opt and /etc as INSTALL.md says.
# Every requirement each distribution in lib/ declares for this Python is in lib/, and no SDK
# that may not be redistributed is. --install also unpacks it as root and runs install.sh on
# this machine (CI): a tree a user owns is refused; the unit is loaded, enabled and not
# started; /etc/vendor-bridges is root's and the service group's; a second run changes nothing.
set -euo pipefail
archive="${1:?usage: packaging/smoke-test.sh <archive.tar.gz> [--install]}"
real_install="${2:-}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
cleanup() { case "$work" in /*/tmp.*) rm -rf -- "$work" ;; esac; }
trap cleanup EXIT
fail() { echo "smoke-test: FAIL: $*" >&2; exit 1; }

tar -xzf "$archive" -C "$work"
top="$(cd "$work" && ls)"
[ "$(printf '%s\n' "$top" | wc -l | tr -d ' ')" = 1 ] || fail "the archive has more than one top directory"
a="$work/$top"
bridge="$(cat "$a/BRIDGE")"
case "$top" in "vendor-bridges-${bridge}_$(cat "$a/VERSION")_linux_"*) ;; *) fail "the top directory $top does not name its bridge and version" ;; esac
grep -qx "$bridge" "$here/redistributable.txt" || fail "$bridge is not a bridge whose SDK may be redistributed"
if find "$a" -type l | grep -q .; then fail "the archive holds a link"; fi
if find "$a" -perm -0002 | grep -q .; then fail "the archive holds a file others can write"; fi

cat > "$work/check.yml" <<YML
mappings:
  - $a/share/mappings
destinations:
  - name: smoke
    adapter: $bridge
YML
# No network: in a network namespace of its own, as this user. An unprivileged user namespace
# where the machine allows one; else, where sudo needs no password (CI, whose runners refuse
# unprivileged user namespaces), a namespace root makes, dropping back to this user. In CI a
# run with the network is a failure; elsewhere it is said and the check goes on.
nonet=()
if unshare -rn true 2>/dev/null; then
  nonet=(unshare -rn)
elif sudo -n unshare -n setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups true 2>/dev/null; then
  nonet=(sudo -n unshare -n setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups)
elif [ -n "${CI:-}" ]; then
  fail "no network namespace can be made here, so the archive cannot be checked offline"
else
  echo "smoke-test: no network namespace here; check runs with the network"
fi
# Prove the namespace has no network before relying on it.
if [ "${#nonet[@]}" -gt 0 ]; then
  "${nonet[@]}" /usr/bin/python3 -I -c 'import socket; socket.create_connection(("1.1.1.1", 53), timeout=3)' 2>/dev/null \
    && fail "the namespace for the check reaches the network"
fi
(cd / && "${nonet[@]}" env -i PATH=/usr/bin:/bin "$a/bin/open-entity-gateway" --help >/dev/null) || fail "the command does not run"
[ ! -e "$a/lib/anduril" ] || fail "the archive carries an SDK that may not be redistributed"
# Every requirement of every distribution, for this Python, is in lib/.
/usr/bin/python3 -I "$here/requires_check.py" "$a/lib" || fail "a declared requirement is missing from lib/"
# A stand-in SDK configuration: validated by the SDK, used for no connection.
(cd / && "${nonet[@]}" env -i PATH=/usr/bin:/bin ECN_AUTH=bearer ECN_BEARER_TOKEN=smoke-test \
  ECN_MQTT_USERNAME=smoke-test ECN_HOST=broker.example.org ECN_INTEGRATION_NAME=smoke-test \
  "$a/bin/open-entity-gateway" check --config "$work/check.yml") | tee "$work/check.out"
grep -qx "destination smoke: $bridge" "$work/check.out" || fail "check did not load the $bridge destination"

for info in "$a"/lib/*.dist-info; do
  n="$(sed -n 's/^Name: //p' "$info/METADATA" | head -1)"
  grep -q "^$n " "$a/licenses/THIRD_PARTY.txt" || fail "licenses/THIRD_PARTY.txt does not name $n"
done

bash "$a/install.sh" --root "$work/root" >/dev/null
opt="$work/root/opt/vendor-bridges/$bridge"
[ "$(readlink "$opt/current")" = "$(cat "$a/VERSION")" ] || fail "current does not name the version"
[ -x "$opt/current/bin/open-entity-gateway" ] || fail "the installed command is missing"
[ -f "$work/root/etc/systemd/system/vendor-bridges-$bridge.service" ] || fail "the unit was not installed"
[ ! -e "$work/root/etc/vendor-bridges/$bridge.yml" ] || fail "the installer wrote a configuration"

if [ "$real_install" = --install ]; then
  # A tree a user unpacked is refused: what install.sh copies becomes code root runs.
  if sudo bash "$a/install.sh" 2>"$work/refused.err"; then fail "install.sh took a tree a user owns"; fi
  grep -q "unpack the archive as root" "$work/refused.err" || fail "install.sh refused for another reason: $(cat "$work/refused.err")"
  ra="$(sudo mktemp -d)"
  sudo tar -xzf "$archive" -C "$ra"
  sudo bash "$ra/$top/install.sh"
  [ "$(sudo stat -c '%U %a' "/opt/vendor-bridges/$bridge/current/lib")" = "root 755" ] || fail "the installed tree is not root's alone"
  [ "$(sudo stat -c '%U %G %a' /etc/vendor-bridges)" = "root vendor-bridges 750" ] || fail "/etc/vendor-bridges is not root's and the group's"
  [ "$(systemctl is-enabled "vendor-bridges-$bridge.service" || true)" = enabled ] || fail "the unit is not enabled"
  [ "$(systemctl show -p LoadState --value "vendor-bridges-$bridge.service")" = loaded ] || fail "systemd did not load the unit"
  [ "$(systemctl is-active "vendor-bridges-$bridge.service" || true)" = inactive ] || fail "the unit started with no configuration"
  sudo -u vendor-bridges env -i PATH=/usr/bin:/bin "/opt/vendor-bridges/$bridge/current/bin/open-entity-gateway" --help >/dev/null \
    || fail "the service user cannot run the command"
  before="$(sudo find /opt/vendor-bridges /etc/vendor-bridges "/etc/systemd/system/vendor-bridges-$bridge.service" -printf '%p %m %U %T@\n' | sort | sha256sum)"
  sudo bash "$ra/$top/install.sh" >/dev/null
  after="$(sudo find /opt/vendor-bridges /etc/vendor-bridges "/etc/systemd/system/vendor-bridges-$bridge.service" -printf '%p %m %U %T@\n' | sort | sha256sum)"
  [ "$before" = "$after" ] || fail "a second install changed the host"
fi
echo "smoke-test: $top passes"
