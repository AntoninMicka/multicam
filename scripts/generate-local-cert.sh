#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${PROJECT_DIR}/certs"
CA_KEY="${CERT_DIR}/local-ca.key.pem"
CA_CERT="${CERT_DIR}/local-ca.cert.pem"
CA_CERT_DER="${CERT_DIR}/local-ca.cert.crt"
SERVER_KEY="${CERT_DIR}/server.key.pem"
SERVER_CERT="${CERT_DIR}/server.cert.pem"
SERVER_CSR="${CERT_DIR}/server.csr.pem"
FORCE=0

if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi

command -v openssl >/dev/null 2>&1 || {
  printf 'Chyba: OpenSSL není nainstalovaný.\n' >&2
  exit 1
}

mkdir -p "${CERT_DIR}"
chmod 700 "${CERT_DIR}"

if [[ ! -f "${CA_KEY}" || ! -f "${CA_CERT}" ]]; then
  printf '[multicam] Vytvářím lokální certifikační autoritu…\n'
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
    -keyout "${CA_KEY}" -out "${CA_CERT}" -days 3650 \
    -subj '/CN=MultiCam Local CA/O=MultiCam'
  chmod 600 "${CA_KEY}"
fi
if [[ ! -f "${CA_CERT_DER}" || "${CA_CERT}" -nt "${CA_CERT_DER}" ]]; then
  openssl x509 -in "${CA_CERT}" -outform DER -out "${CA_CERT_DER}"
fi

declare -a ip_addresses=("127.0.0.1")
if [[ -n "${MULTICAM_CERT_IPS:-}" ]]; then
  IFS=',' read -r -a configured_ips <<< "${MULTICAM_CERT_IPS}"
  ip_addresses+=("${configured_ips[@]}")
else
  read -r -a detected_ips <<< "$(hostname -I 2>/dev/null || true)"
  ip_addresses+=("${detected_ips[@]}")
fi

san='DNS:localhost,IP:127.0.0.1'
for address in "${ip_addresses[@]}"; do
  [[ -z "${address}" || "${address}" == "127.0.0.1" ]] && continue
  if [[ "${address}" =~ ^[0-9a-fA-F:.]+$ ]]; then
    san+=",IP:${address}"
  else
    printf 'Chyba: neplatná IP adresa pro certifikát: %s\n' "${address}" >&2
    exit 1
  fi
done

certificate_matches=1
if [[ ${FORCE} -ne 0 || ! -f "${SERVER_KEY}" || ! -f "${SERVER_CERT}" ]]; then
  certificate_matches=0
elif ! openssl x509 -checkend 604800 -noout -in "${SERVER_CERT}" >/dev/null \
  || ! openssl x509 -checkhost localhost -noout -in "${SERVER_CERT}" >/dev/null; then
  certificate_matches=0
else
  for address in "${ip_addresses[@]}"; do
    if [[ -n "${address}" ]] && ! openssl x509 -checkip "${address}" -noout -in "${SERVER_CERT}" >/dev/null; then
      certificate_matches=0
      break
    fi
  done
fi

if [[ ${certificate_matches} -eq 1 ]]; then
  printf '[multicam] Platný lokální certifikát pro aktuální adresy už existuje.\n'
  exit 0
fi

extensions_file="$(mktemp)"
trap 'rm -f -- "${extensions_file}" "${SERVER_CSR}"' EXIT
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "${san}" > "${extensions_file}"

printf '[multicam] Vytvářím serverový certifikát pro %s…\n' "${san}"
openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -keyout "${SERVER_KEY}" -out "${SERVER_CSR}" \
  -subj '/CN=multicam.local/O=MultiCam'
openssl x509 -req -sha256 -in "${SERVER_CSR}" \
  -CA "${CA_CERT}" -CAkey "${CA_KEY}" -CAcreateserial \
  -out "${SERVER_CERT}" -days 825 -extfile "${extensions_file}"
chmod 600 "${SERVER_KEY}"

printf '[multicam] Certifikát je připravený. Do telefonů nainstalujte:\n%s\n' "${CA_CERT_DER}"
