#!/usr/bin/env bash
# The gateway's fabric destination delivered by octo-pod's egress, on one host.
#
#     tests/e2e/octo-pod-egress/run.sh <octo-pod checkout>
#
# Builds octo-pod's downlink-bridge, egress-arm and sapient arm (cargo), makes
# a throwaway CA and leaf certificates (openssl), and runs run.py with the Python
# that has this repository installed with the zenoh extra (OEG_PYTHON, default
# python3). Run from the repository root, after tools/fetch-cot-xsd.py and
# tools/fetch-sapient-proto.py. Listens on 127.0.0.1 only: the router on 17447,
# the receivers on 18610 to 18612 and the SAPIENT controller on 18620.
# Everything it writes is removed on exit.
set -euo pipefail

POD="${1:?usage: $0 <octo-pod checkout>}"
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cargo build --release --quiet --manifest-path "$POD/arms/Cargo.toml" -p octo-egress \
  --bin egress-arm --bin downlink-bridge
cargo build --release --quiet --manifest-path "$POD/arms/Cargo.toml" -p sapient --bin sapient

mkdir -p "$WORK/tls"
cd "$WORK/tls"
openssl ecparam -name prime256v1 -genkey -noout -out ca-key.pem
openssl req -x509 -new -key ca-key.pem -subj "/CN=oeg-e2e-ca" -days 1 -sha256 -out ca.pem
for name in router gateway bridge arm tap; do
  openssl ecparam -name prime256v1 -genkey -noout -out "$name-key.pem"
  openssl req -new -key "$name-key.pem" -subj "/CN=$name" -out "$name.csr"
  openssl x509 -req -in "$name.csr" -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 1 -sha256 \
    -extfile <(printf "subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE") \
    -out "$name-cert.pem" 2>/dev/null
done
cd "$ROOT"

"${OEG_PYTHON:-python3}" tests/e2e/octo-pod-egress/run.py "$WORK/tls" "$WORK" "$POD/arms/target/release"
