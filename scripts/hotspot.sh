#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"
CONNECTION_NAME='multicam-managed-hotspot'
RUN_DIR='/run/multicam'
HOTSPOT_ADDRESS="${MULTICAM_HOTSPOT_ADDRESS:-10.42.0.1}"
HOTSPOT_SSID="${MULTICAM_HOTSPOT_SSID:-MultiCam}"
HOTSPOT_PASSWORD="${MULTICAM_HOTSPOT_PASSWORD:-multicam-local}"
APP_PORT="${MULTICAM_PORT:-8000}"

fail() { printf 'Chyba: %s\n' "$*" >&2; exit 1; }

if [[ ${EUID} -ne 0 ]]; then
  exec sudo --preserve-env=MULTICAM_WIFI_IFACE,MULTICAM_HOTSPOT_ADDRESS,MULTICAM_HOTSPOT_SSID,MULTICAM_HOTSPOT_PASSWORD,MULTICAM_PORT \
    "$0" "${ACTION}"
fi

[[ "${HOTSPOT_ADDRESS}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail 'Neplatná IPv4 adresa hotspotu.'
[[ "${HOTSPOT_SSID}" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || fail 'SSID smí obsahovat pouze písmena, číslice, _ a -.'
[[ "${HOTSPOT_PASSWORD}" =~ ^[A-Za-z0-9_-]{8,63}$ ]] || fail 'Heslo musí mít 8–63 znaků: písmena, číslice, _ nebo -.'

stop_hotspot() {
  if [[ -r "${RUN_DIR}/portal.pid" ]]; then
    portal_pid="$(<"${RUN_DIR}/portal.pid")"
    [[ "${portal_pid}" =~ ^[0-9]+$ ]] && kill "${portal_pid}" 2>/dev/null || true
  fi
  if [[ -r "${RUN_DIR}/dnsmasq.pid" ]]; then
    dnsmasq_pid="$(<"${RUN_DIR}/dnsmasq.pid")"
    [[ "${dnsmasq_pid}" =~ ^[0-9]+$ ]] && kill "${dnsmasq_pid}" 2>/dev/null || true
  fi
  nft delete table inet multicam 2>/dev/null || true
  nmcli connection down "${CONNECTION_NAME}" >/dev/null 2>&1 || true
  nmcli connection delete "${CONNECTION_NAME}" >/dev/null 2>&1 || true
  rm -f -- "${RUN_DIR}/portal.pid" "${RUN_DIR}/dnsmasq.pid" "${RUN_DIR}/dnsmasq.conf" "${RUN_DIR}/hotspot.json"
  printf '[multicam] Hotspot je vypnutý.\n'
}

case "${ACTION}" in
  stop) stop_hotspot; exit 0 ;;
  status)
    if [[ -r "${RUN_DIR}/hotspot.json" ]]; then cat "${RUN_DIR}/hotspot.json"; else printf '{"active":false}\n'; fi
    exit 0 ;;
  start) ;;
  *) fail 'Použití: hotspot.sh start|stop|status' ;;
esac

for command in nmcli dnsmasq nft python3; do command -v "${command}" >/dev/null || fail "Chybí příkaz ${command}."; done
[[ -r "${PROJECT_DIR}/certs/local-ca.cert.pem" ]] || fail 'Nejprve vygenerujte lokální HTTPS certifikát.'

WIFI_IFACE="${MULTICAM_WIFI_IFACE:-}"
if [[ -z "${WIFI_IFACE}" ]]; then
  WIFI_IFACE="$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2 == "wifi" { print $1; exit }')"
fi
[[ "${WIFI_IFACE}" =~ ^[A-Za-z0-9_.-]+$ ]] || fail 'Wi-Fi rozhraní nebylo nalezeno; nastavte MULTICAM_WIFI_IFACE.'

mkdir -p "${RUN_DIR}"
chmod 755 "${RUN_DIR}"
if nmcli -t -f NAME connection show | grep -Fxq "${CONNECTION_NAME}"; then
  fail "Připojení ${CONNECTION_NAME} už existuje; nejprve spusťte hotspot.sh stop."
fi

cleanup_on_error() { stop_hotspot; }
trap cleanup_on_error ERR

nmcli connection add type wifi ifname "${WIFI_IFACE}" con-name "${CONNECTION_NAME}" ssid "${HOTSPOT_SSID}" >/dev/null
nmcli connection modify "${CONNECTION_NAME}" \
  connection.autoconnect no 802-11-wireless.mode ap 802-11-wireless.band bg \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${HOTSPOT_PASSWORD}" \
  ipv4.method manual ipv4.addresses "${HOTSPOT_ADDRESS}/24" ipv4.never-default yes ipv6.method disabled
nmcli connection up "${CONNECTION_NAME}" >/dev/null

cat > "${RUN_DIR}/dnsmasq.conf" <<EOF
interface=${WIFI_IFACE}
bind-interfaces
listen-address=${HOTSPOT_ADDRESS}
dhcp-range=10.42.0.10,10.42.0.200,255.255.255.0,12h
dhcp-option=3,${HOTSPOT_ADDRESS}
dhcp-option=6,${HOTSPOT_ADDRESS}
address=/#/${HOTSPOT_ADDRESS}
no-resolv
no-hosts
log-dhcp
EOF
dnsmasq --conf-file="${RUN_DIR}/dnsmasq.conf" --pid-file="${RUN_DIR}/dnsmasq.pid"

nft add table inet multicam
nft 'add chain inet multicam input { type filter hook input priority -10; policy accept; }'
nft 'add chain inet multicam forward { type filter hook forward priority -10; policy accept; }'
nft add rule inet multicam input iifname "${WIFI_IFACE}" udp dport { 53, 67 } accept
nft add rule inet multicam input iifname "${WIFI_IFACE}" tcp dport { 53, 80, "${APP_PORT}" } accept
nft add rule inet multicam forward iifname "${WIFI_IFACE}" drop

owner_uid="${SUDO_UID:-0}"
owner_gid="${SUDO_GID:-0}"
python3 "${PROJECT_DIR}/scripts/captive_portal.py" \
  --bind "${HOTSPOT_ADDRESS}" --port 80 --ssid "${HOTSPOT_SSID}" \
  --app-url "https://${HOTSPOT_ADDRESS}:${APP_PORT}/" \
  --ca-cert "${PROJECT_DIR}/certs/local-ca.cert.pem" --uid "${owner_uid}" --gid "${owner_gid}" &
portal_pid=$!
printf '%s\n' "${portal_pid}" > "${RUN_DIR}/portal.pid"

printf '{"active":true,"ssid":"%s","password":"%s","address":"%s","app_url":"https://%s:%s/","interface":"%s"}\n' \
  "${HOTSPOT_SSID}" "${HOTSPOT_PASSWORD}" "${HOTSPOT_ADDRESS}" "${HOTSPOT_ADDRESS}" "${APP_PORT}" "${WIFI_IFACE}" > "${RUN_DIR}/hotspot.json"
chmod 644 "${RUN_DIR}/hotspot.json"
trap - ERR
printf '[multicam] Hotspot %s běží na %s (%s).\n' "${HOTSPOT_SSID}" "${HOTSPOT_ADDRESS}" "${WIFI_IFACE}"

