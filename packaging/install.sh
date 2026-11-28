#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Install the vendor bridge this release archive carries, from the archive unpacked as root:
#
#   sudo tar -xzf <archive> -C <a directory only root can write>
#   sudo <that directory>/<archive's top directory>/install.sh [--root DIR] [--no-systemd]
#
# Puts the archive's tree at /opt/vendor-bridges/<bridge>/<version>/ (root's, writable by
# root alone) and points /opt/vendor-bridges/<bridge>/current at it, keeping the version
# before as `previous`. Creates the system user vendor-bridges, /etc/vendor-bridges/ (root's,
# readable by the user's group) and the service unit vendor-bridges-<bridge>.service, which it
# enables. It writes no configuration (INSTALL.md). Once the configuration is in place, the
# service is restarted on the version installed; if it does not stay up, `current` goes back to
# the version before and that one is restarted, and the run fails. Needs nothing from the
# network. Running an earlier archive's install.sh rolls back to it.
#
# It refuses an unpacked tree that anyone but root owns or can change: what it copies becomes
# code root installs and the service runs.
#
# --root DIR   install under DIR instead of / (a test or an image build); implies --no-systemd
# --no-systemd install the files only: no user, no daemon reload, no enable or restart
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root=/
systemd=1
while [ $# -gt 0 ]; do
  case "$1" in
    --root) root="${2:?--root needs a directory}"; systemd=0; shift 2 ;;
    --no-systemd) systemd=0; shift ;;
    -h | --help) sed -n '3,23p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

die() { echo "install.sh: $*; nothing was installed" >&2; exit 1; }
bridge="$(cat "$here/BRIDGE")"
version="$(cat "$here/VERSION")"
want_py="$(cat "$here/PYTHON")"
printf '%s\n' "$bridge" | grep -Eqx '[a-z0-9]+(-[a-z0-9]+)*' || die "the archive names no valid bridge"
printf '%s\n' "$version" | grep -Eqx '[0-9]+(\.[0-9]+)*([+-][0-9A-Za-z.]+)?' || die "the archive names no valid version"

if [ "$root" = / ]; then
  [ "$(id -u)" = 0 ] || die "run it as root (sudo)"
  have_py="$(/usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
    || die "/usr/bin/python3 is missing"
  [ "$have_py" = "$want_py" ] || die "the archive is built for Python $want_py and /usr/bin/python3 is $have_py"
  case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) arch="$(uname -m)" ;; esac
  case "$(basename "$here")" in *"_linux_$arch") ;; *) die "this archive is not for $arch" ;; esac
  # The unpacked tree and the directory above it: root's, and writable by root alone.
  # -quit, not a pipe into head: under pipefail a find cut short by head fails the script.
  bad="$(find "$here" "$(dirname "$here")" -maxdepth 0 \( ! -user 0 -o -perm /022 \) -print -quit)"
  [ -n "$bad" ] || bad="$(find "$here" \( ! -user 0 -o -perm /022 \) -print -quit)"
  [ -z "$bad" ] || die "unpack the archive as root into a directory only root can write; not root's alone: $bad"
fi

opt="${root%/}/opt/vendor-bridges/$bridge"
etc="${root%/}/etc/vendor-bridges"
unit_dir="${root%/}/etc/systemd/system"
unit="vendor-bridges-$bridge.service"
dest="$opt/$version"

install -d -m 0755 "${root%/}/opt/vendor-bridges" "$opt"
changed=0
if [ ! -d "$dest" ]; then
  partial="$opt/.$version.partial"
  if [ -e "$partial" ]; then
    # Left by an interrupted run of this installer: root's, under root's directory.
    case "$partial" in "$opt"/.*.partial) rm -rf -- "$partial" ;; esac
  fi
  cp -R "$here" "$partial"
  if [ "$root" = / ]; then chown -R 0:0 "$partial"; fi
  chmod -R u+rwX,go+rX,go-w "$partial"
  mv "$partial" "$dest"
  changed=1
elif ! diff -rq "$here" "$dest" >/dev/null; then
  die "$dest exists and differs from this archive: a version is installed once; a fix is a new version"
fi

current=""
[ -L "$opt/current" ] && current="$(readlink "$opt/current")"
if [ "$current" != "$version" ]; then
  [ -n "$current" ] && ln -sfn -- "$current" "$opt/previous"
  ln -sfn -- "$version" "$opt/.current.new"
  mv -T "$opt/.current.new" "$opt/current"
  changed=1
fi

if [ "$systemd" = 1 ]; then
  getent group vendor-bridges >/dev/null || groupadd --system vendor-bridges
  getent passwd vendor-bridges >/dev/null \
    || useradd --system --gid vendor-bridges --home-dir /var/lib/vendor-bridges --no-create-home --shell /usr/sbin/nologin vendor-bridges
fi
install -d -m 0750 "$etc"
[ -e "$etc/$bridge.env" ] || install -m 0640 /dev/null "$etc/$bridge.env"
if [ "$systemd" = 1 ]; then chown 0:vendor-bridges "$etc" "$etc/$bridge.env"; fi
install -d -m 0755 "$unit_dir"
if ! cmp -s "$dest/systemd/$unit" "$unit_dir/$unit"; then
  install -m 0644 "$dest/systemd/$unit" "$unit_dir/$unit"
  changed=1
fi

if [ "$systemd" = 1 ]; then
  systemctl daemon-reload
  systemctl enable "$unit" >/dev/null
  if [ ! -f "$etc/$bridge.yml" ] || [ ! -f "$etc/fabric.json5" ] || ! grep -q '^VENDOR_BRIDGES_KEYS=.*--key' "$etc/$bridge.env"; then
    echo "install.sh: $bridge $version installed; it is not started until $etc/$bridge.yml, $etc/fabric.json5 and VENDOR_BRIDGES_KEYS in $etc/$bridge.env are set (INSTALL.md), then: systemctl restart $unit"
  elif [ "$changed" = 1 ] || ! systemctl is-active --quiet "$unit"; then
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl restart "$unit" || true
    sleep 10
    if [ "$(systemctl show -p NRestarts --value "$unit")" != 0 ] || ! systemctl is-active --quiet "$unit"; then
      prev="$(readlink "$opt/previous" 2>/dev/null || true)"
      if [ -n "$prev" ] && [ "$prev" != "$version" ] && [ -d "$opt/$prev" ]; then
        ln -sfn -- "$prev" "$opt/.current.new"
        mv -T "$opt/.current.new" "$opt/current"
        install -m 0644 "$opt/$prev/systemd/$unit" "$unit_dir/$unit"
        systemctl daemon-reload
        systemctl reset-failed "$unit" 2>/dev/null || true
        systemctl restart "$unit" || true
        echo "install.sh: $bridge $version did not stay up; current is back on $prev (journalctl -u $unit)" >&2
      else
        echo "install.sh: $bridge $version did not stay up (journalctl -u $unit)" >&2
      fi
      exit 1
    fi
    echo "install.sh: $bridge $version is running"
  fi
fi
echo "install.sh: $bridge $version is current at $opt/current"
