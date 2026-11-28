#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build one bridge's release archive for this machine's architecture:
#
#   packaging/build-archive.sh <bridge> [<output directory>]     (default: dist)
#
# <bridge> is a directory under vendors/ whose SDK may be redistributed (RELEASING.md).
# The archive is vendor-bridges-<bridge>_<version>_linux_<arch>.tar.gz, with one top
# directory of the same name holding:
#   lib/          the core, its senders, the bridge and every dependency, installed from
#                 the hash-locked requirements files (wheels only), for this Python
#   bin/open-entity-gateway   the command, run with the system's python3 (-I)
#   share/        the mapping files and the example configuration
#   systemd/      the service unit; install.sh installs it
#   licenses/     every distribution's licence files, and THIRD_PARTY.txt naming each one
#   PYTHON, VERSION, BRIDGE   what it was built for
#   install.sh, INSTALL.md
# Built with the python3 the host will run it with: the archive's native wheels are for
# that Python only, and the command refuses another. No network is needed to run it.
set -euo pipefail

bridge="${1:?usage: packaging/build-archive.sh <bridge> [<output directory>]}"
out="${2:-dist}"
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$bridge" in
  */* | .* | "") echo "build-archive: not a bridge name: $bridge" >&2; exit 2 ;;
esac
[ -f "$root/vendors/$bridge/pyproject.toml" ] || { echo "build-archive: no vendors/$bridge/pyproject.toml" >&2; exit 2; }
grep -qx "$bridge" "$root/packaging/redistributable.txt" || { echo "build-archive: $bridge is not in packaging/redistributable.txt" >&2; exit 2; }
[ -f "$root/vendors/$bridge/requirements.txt" ] || { echo "build-archive: no vendors/$bridge/requirements.txt (a hash-locked SDK)" >&2; exit 2; }

py=python3
version="$("$py" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$root/vendors/$bridge/pyproject.toml")"
pyver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
  *) echo "build-archive: unsupported machine $(uname -m)" >&2; exit 2 ;;
esac
name="vendor-bridges-${bridge}_${version}_linux_${arch}"

work="$(mktemp -d)"
cleanup() { case "$work" in /*/tmp.*) rm -rf -- "$work" ;; esac; }
trap cleanup EXIT

echo "build-archive: $name for Python $pyver"
# The commit's time names every file, so a build of one commit gives the same bytes.
SOURCE_DATE_EPOCH="$(git -C "$root" log -1 --format=%ct 2>/dev/null || echo 0)"
export SOURCE_DATE_EPOCH
"$py" -m venv "$work/build"
"$work/build/bin/python" -m pip install -q --require-hashes -r "$root/requirements/build.txt" -r "$root/requirements/tools.txt"
(cd "$root" && "$work/build/bin/python" tools/fetch-sapient-proto.py)
for package in "$root" "$root/vendors/$bridge"; do
  "$work/build/bin/python" -m build -q --no-isolation --wheel --outdir "$work/wheels" "$package"
done

stage="$work/$name"
mkdir -p "$stage/lib" "$stage/bin" "$stage/share" "$stage/systemd" "$stage/licenses"
# Every dependency from the hash-locked files, as wheels for this Python and machine.
"$work/build/bin/python" -m pip install -q --no-compile --no-deps --only-binary :all: --require-hashes \
  --target "$stage/lib" -r "$root/requirements/runtime.txt" -r "$root/vendors/$bridge/requirements.txt"
# The core and the bridge, built here from this commit.
"$work/build/bin/python" -m pip install -q --no-compile --no-deps --no-index --target "$stage/lib" "$work"/wheels/*.whl
find "$stage/lib" -name '__pycache__' -prune -exec rm -rf {} +
# Console scripts pip writes name the build environment's interpreter: bin/ replaces them.
# direct_url.json names the build's temporary directory. Neither ships, and RECORD drops them,
# so a build of one commit gives the same bytes.
rm -rf "$stage/lib/bin"
find "$stage/lib" -path '*.dist-info/direct_url.json' -delete
"$py" -I - "$stage/lib" <<'PY'
import pathlib, sys
for record in pathlib.Path(sys.argv[1]).glob("*.dist-info/RECORD"):
    keep = [l for l in record.read_text(encoding="utf-8").splitlines(keepends=True)
            if not l.startswith("../../bin/") and not l.split(",", 1)[0].endswith(".dist-info/direct_url.json")]
    record.write_text("".join(keep), encoding="utf-8")
PY
[ -f "$stage/lib/open_entity_gateway/adapters/sapient/_generated/LICENCE.txt" ] || { echo "build-archive: the SAPIENT bindings are missing" >&2; exit 1; }

cp "$root/packaging/open-entity-gateway" "$stage/bin/open-entity-gateway"
chmod 0755 "$stage/bin/open-entity-gateway"
cp -R "$root/mappings" "$stage/share/mappings"
cp "$root/examples/gateway.yml" "$stage/share/gateway.example.yml"
sed "s/@BRIDGE@/$bridge/g" "$root/packaging/vendor-bridges.service.in" > "$stage/systemd/vendor-bridges-$bridge.service"
cp "$root/packaging/install.sh" "$stage/install.sh"
chmod 0755 "$stage/install.sh"
cp "$root/packaging/INSTALL.md" "$stage/INSTALL.md"
printf '%s\n' "$pyver" > "$stage/PYTHON"
printf '%s\n' "$version" > "$stage/VERSION"
printf '%s\n' "$bridge" > "$stage/BRIDGE"
cp "$root/LICENSE" "$root/NOTICE" "$stage/licenses/"
"$py" -I "$root/packaging/third_party.py" "$stage/lib" "$stage/licenses"

mkdir -p "$out"
epoch="$SOURCE_DATE_EPOCH"
find "$stage" -exec touch -h -d "@$epoch" {} +
(cd "$work" && tar --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' -cf - "$name") \
  | gzip -n -9 > "$out/$name.tar.gz"
echo "build-archive: $out/$name.tar.gz ($(wc -c < "$out/$name.tar.gz" | tr -d ' ') bytes)"
