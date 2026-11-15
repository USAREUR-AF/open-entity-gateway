#!/usr/bin/env bash
# Start OpenTAKServer and Dstl's Apex in containers, run tests/e2e against
# them from a runner container on the same network, then remove everything.
#
#     tests/e2e/run-on-build-host.sh
#
# Needs docker with compose and git. Publishes no port on the host. Run from
# the repository root. OEG_E2E_KEEP=1 leaves the containers up.
set -euo pipefail

OTS_DOCKER_COMMIT=30aa20b4c9575f338987aa7bd568b0fe9ec258f0   # brian7704/OpenTAKServer-Docker, 2026-11-11
WORK="${OEG_E2E_DIR:-$HOME/oeg-e2e-work}"
PROJECT=oeg-e2e
NETWORK="${PROJECT}_default"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# shellcheck disable=SC2317,SC2329  # called by the EXIT trap; older and newer shellcheck name it differently
cleanup() {
  if [ "${OEG_E2E_KEEP:-0}" = 1 ]; then return; fi
  docker rm -f oeg-e2e-apex >/dev/null 2>&1 || true
  if [ -f "$WORK/ots/docker-compose.yml" ]; then
    docker compose -p "$PROJECT" -f "$WORK/ots/docker-compose.yml" -f "$ROOT/tests/e2e/opentakserver/override.yml" \
      down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  docker image rm oeg-e2e-apex oeg-e2e-runner >/dev/null 2>&1 || true
  # The OpenTAKServer volume is written by root inside the container.
  docker run --rm -v "$WORK:/w" alpine:3 rm -rf /w/ots >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK"
git clone -q https://github.com/brian7704/OpenTAKServer-Docker.git "$WORK/ots"
git -C "$WORK/ots" checkout -q "$OTS_DOCKER_COMMIT"

echo "== OpenTAKServer"
docker compose -p "$PROJECT" -f "$WORK/ots/docker-compose.yml" -f "$ROOT/tests/e2e/opentakserver/override.yml" \
  up -d --wait ots ots_cot_parser ots_eud_handler ots_eud_handler_ssl rabbitmq ots-db
docker compose -p "$PROJECT" -f "$WORK/ots/docker-compose.yml" images

echo "== Apex"
docker build -q -t oeg-e2e-apex "$ROOT/tests/e2e/apex"
docker run -d --name oeg-e2e-apex --network "$NETWORK" oeg-e2e-apex >/dev/null

echo "== runner"
docker build -q -t oeg-e2e-runner -f "$ROOT/tests/e2e/runner.Dockerfile" "$ROOT"
sleep 5
status=0
docker run --rm --network "$NETWORK" \
  -v "$WORK/ots/ots/ca:/ots-ca:ro" \
  -e OEG_E2E_TAK=ots_eud_handler:8088 \
  -e OEG_E2E_TAK_TLS=ots_eud_handler_ssl:8089 \
  -e OEG_E2E_TAK_CA=/ots-ca \
  -e OEG_E2E_APEX=oeg-e2e-apex \
  oeg-e2e-runner || status=$?

echo "== Apex log (validation and errors)"
docker logs oeg-e2e-apex 2>&1 | grep -E -i "error|validation|registration" | tail -n 40 || true
exit "$status"
