#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf 'Použití: sudo %s --install [NETWORK_ID]\n' "${0##*/}"
  printf '         sudo %s NETWORK_ID\n' "${0##*/}"
  printf 'Nainstaluje ZeroTier a volitelně připojí existující síť.\n'
}

install=0
if [[ "${1:-}" == "--install" ]]; then install=1; shift; fi
network_id="${1:-}"
if [[ -z "${network_id}" && ${install} -ne 1 ]]; then usage >&2; exit 2; fi
if [[ -n "${network_id}" && ! "${network_id}" =~ ^[0-9a-fA-F]{16}$ ]]; then usage >&2; exit 2; fi
if [[ ${EUID} -ne 0 ]]; then printf 'Chyba: spusťte skript přes sudo.\n' >&2; exit 1; fi
if [[ ! -r /etc/os-release ]]; then printf 'Chyba: nelze určit distribuci.\n' >&2; exit 1; fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  ubuntu:*|debian:*|*:debian*) ;;
  *) printf 'Chyba: podporované jsou pouze Debian a Ubuntu.\n' >&2; exit 1 ;;
esac

if ! command -v zerotier-cli >/dev/null 2>&1; then
  if [[ ${install} -ne 1 ]]; then
    printf 'Chyba: ZeroTier není nainstalovaný. Spusťte znovu s --install.\n' >&2
    exit 1
  fi
  apt-get update
  apt-get install -y ca-certificates curl gpg
  installer="$(mktemp)"
  trap 'rm -f -- "${installer}"' EXIT
  curl --fail --proto '=https' --tlsv1.2 https://install.zerotier.com/ -o "${installer}"
  bash "${installer}"
fi

systemctl enable --now zerotier-one
if [[ -n "${network_id}" ]]; then
  zerotier-cli join "${network_id,,}"
  printf '\nZeroTier čeká na autorizaci člena v ZeroTier Central. Stav:\n'
fi
zerotier-cli listnetworks
if [[ -n "${network_id}" ]]; then
  printf '\nPo autorizaci spusťte MultiCam s MULTICAM_PUBLIC_URL=https://ZEROTIER_IP:8000 ./run.sh\n'
fi
