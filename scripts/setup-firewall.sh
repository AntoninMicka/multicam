#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-apply}"
APP_PORT="${MULTICAM_PORT:-8000}"
DISCOVERY_PORT="${MULTICAM_DISCOVERY_PORT:-47777}"
COMMENT_PREFIX='MultiCam managed'

fail() { printf 'Chyba: %s\n' "$*" >&2; exit 1; }
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }

valid_port "${APP_PORT}" || fail "Neplatný MULTICAM_PORT: ${APP_PORT}"
valid_port "${DISCOVERY_PORT}" || fail "Neplatný MULTICAM_DISCOVERY_PORT: ${DISCOVERY_PORT}"
[[ "${ACTION}" == apply || "${ACTION}" == remove || "${ACTION}" == status ]] \
  || fail 'Použití: setup-firewall.sh [apply|remove|status]'

if ! command -v ufw >/dev/null 2>&1; then
  printf '[multicam] UFW není nainstalovaný; automatická konfigurace firewallu byla přeskočena.\n'
  exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
  exec sudo --preserve-env=MULTICAM_PORT,MULTICAM_DISCOVERY_PORT "$0" "${ACTION}"
fi

ufw_active=0
if ufw status 2>/dev/null | grep -qi '^Status: active'; then ufw_active=1; fi

if [[ "${ACTION}" == status ]]; then
  if (( ufw_active == 0 )); then
    printf '[multicam] UFW je neaktivní.\n'
  else
    ufw status | grep -F "${COMMENT_PREFIX}" || printf '[multicam] UFW je aktivní, ale pravidla MultiCam chybí.\n'
  fi
  exit 0
fi

if [[ "${ACTION}" == apply && ${ufw_active} -eq 0 ]]; then
  printf '[multicam] UFW je neaktivní; firewall nebyl zapnut ani jinak změněn.\n'
  exit 0
fi

delete_rule() {
  local protocol="$1" port="$2" comment="$3"
  while ufw show added | grep -F "${port}/${protocol}" | grep -Fq "comment '${comment}'"; do
    ufw --force delete allow proto "${protocol}" to any port "${port}" comment "${comment}" >/dev/null
  done
}

web_comment="${COMMENT_PREFIX} web"
discovery_comment="${COMMENT_PREFIX} discovery"
delete_rule tcp "${APP_PORT}" "${web_comment}"
delete_rule udp "${DISCOVERY_PORT}" "${discovery_comment}"

if [[ "${ACTION}" == remove ]]; then
  printf '[multicam] Spravovaná pravidla UFW byla odebrána.\n'
  exit 0
fi

ufw allow proto tcp to any port "${APP_PORT}" comment "${web_comment}" >/dev/null
ufw allow proto udp to any port "${DISCOVERY_PORT}" comment "${discovery_comment}" >/dev/null
printf '[multicam] UFW povoluje TCP %s (web/API) a UDP %s (discovery).\n' \
  "${APP_PORT}" "${DISCOVERY_PORT}"
