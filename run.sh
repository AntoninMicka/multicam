#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
FRONTEND_DIR="${PROJECT_DIR}/frontend"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MULTICAM_HOST="${MULTICAM_HOST:-0.0.0.0}"
MULTICAM_PORT="${MULTICAM_PORT:-8000}"

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

log "Spouštím server na http://${MULTICAM_HOST}:${MULTICAM_PORT}"
cd "${PROJECT_DIR}"
exec "${VENV_DIR}/bin/python" -m uvicorn backend.app.main:app \
  --host "${MULTICAM_HOST}" \
  --port "${MULTICAM_PORT}" \
  "$@"

