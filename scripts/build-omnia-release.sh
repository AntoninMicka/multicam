#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
release_path="${1:-/tmp/multicam-omnia.tar.gz}"
npm --prefix "${project_dir}/frontend" ci
npm --prefix "${project_dir}/frontend" run build
# Ship source and static assets only: no credentials, recordings, venv or native x86 modules.
tar -C "${project_dir}" --exclude='__pycache__' --exclude='*.pyc' -czf "${release_path}" \
  backend/app frontend/dist deploy/omnia
printf 'Připraveno: %s\n' "${release_path}"
