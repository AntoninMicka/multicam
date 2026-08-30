#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
FRONTEND_DIR="${PROJECT_DIR}/frontend"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MULTICAM_HOST="${MULTICAM_HOST:-0.0.0.0}"
MULTICAM_PORT="${MULTICAM_PORT:-8000}"
MULTICAM_HTTPS="${MULTICAM_HTTPS:-1}"
MULTICAM_CERT="${MULTICAM_CERT:-${PROJECT_DIR}/certs/server.cert.pem}"
MULTICAM_KEY="${MULTICAM_KEY:-${PROJECT_DIR}/certs/server.key.pem}"

log() {
  printf '[multicam] %s\n' "$*"
}

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || {
  printf 'Chyba: příkaz %s nebyl nalezen.\n' "${PYTHON_BIN}" >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  printf 'Chyba: npm nebylo nalezeno. Nainstalujte Node.js a npm.\n' >&2
  exit 1
}
command -v ffmpeg >/dev/null 2>&1 || {
  printf 'Chyba: ffmpeg nebyl nalezen. Je potřeba pro kompatibilní přehrávací kopie WebM.\n' >&2
  exit 1
}

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  log "Vytvářím Python virtualenv…"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

if ! "${VENV_DIR}/bin/python" -c 'import fastapi, uvicorn' >/dev/null 2>&1 \
  || [[ "${PROJECT_DIR}/backend/requirements.txt" -nt "${VENV_DIR}/.requirements-installed" ]]; then
  log "Instaluji Python závislosti…"
  "${VENV_DIR}/bin/python" -m pip install -r "${PROJECT_DIR}/backend/requirements.txt"
  touch "${VENV_DIR}/.requirements-installed"
fi

if [[ ! -d "${FRONTEND_DIR}/node_modules" ]] \
  || [[ "${FRONTEND_DIR}/package-lock.json" -nt "${FRONTEND_DIR}/node_modules/.package-lock.json" ]]; then
  log "Instaluji frontendové závislosti…"
  if [[ -f "${FRONTEND_DIR}/package-lock.json" ]]; then
    npm --prefix "${FRONTEND_DIR}" ci
  else
    npm --prefix "${FRONTEND_DIR}" install
  fi
fi

log "Sestavuji PWA…"
npm --prefix "${FRONTEND_DIR}" run build

declare -a ssl_args=()
scheme='http'
if [[ "${MULTICAM_HTTPS}" != "0" ]]; then
  if [[ "${MULTICAM_CERT}" == "${PROJECT_DIR}/certs/server.cert.pem" \
    && "${MULTICAM_KEY}" == "${PROJECT_DIR}/certs/server.key.pem" ]]; then
    "${PROJECT_DIR}/scripts/generate-local-cert.sh"
  fi
  if [[ ! -r "${MULTICAM_CERT}" || ! -r "${MULTICAM_KEY}" ]]; then
    printf 'Chyba: certifikát nebo privátní klíč není čitelný.\n' >&2
    exit 1
  fi
  ssl_args=(--ssl-certfile "${MULTICAM_CERT}" --ssl-keyfile "${MULTICAM_KEY}")
  scheme='https'
fi

log "Spouštím server na ${scheme}://${MULTICAM_HOST}:${MULTICAM_PORT}"
cd "${PROJECT_DIR}"
exec "${VENV_DIR}/bin/python" -m uvicorn backend.app.main:app \
  --host "${MULTICAM_HOST}" \
  --port "${MULTICAM_PORT}" \
  "${ssl_args[@]}" \
  "$@"
