#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOTSPOT_ADDRESS="${MULTICAM_HOTSPOT_ADDRESS:-10.42.0.1}"

MULTICAM_CERT_IPS="${HOTSPOT_ADDRESS}" "${PROJECT_DIR}/scripts/generate-local-cert.sh"
"${PROJECT_DIR}/scripts/hotspot.sh" start

server_pid=''
cleanup() {
  if [[ -n "${server_pid}" ]]; then kill "${server_pid}" 2>/dev/null || true; fi
  "${PROJECT_DIR}/scripts/hotspot.sh" stop || true
}
trap cleanup EXIT INT TERM

MULTICAM_HOST=0.0.0.0 "${PROJECT_DIR}/run.sh" "$@" &
server_pid=$!
wait "${server_pid}"

