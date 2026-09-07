#!/bin/sh
# Run inside the LXC: update-container.sh /srv/multicam-ssd/multicam-omnia.tar.gz
set -eu
[ "$(id -u)" = 0 ] || { echo 'Spusťte jako root uvnitř LXC.' >&2; exit 1; }
archive=${1:?Zadejte cestu k release archivu}
archive=$(readlink -f "$archive")
set -a
. /etc/multicam.env
set +a
cd /srv/multicam-ssd/app
python3 -m backend.app.storage_guard
staging=$(mktemp -d /srv/multicam-ssd/.update-XXXXXX)
trap 'rm -rf "$staging"' EXIT HUP INT TERM
python3 - "$archive" "$staging" <<'PY'
import ast
import pathlib
import sys
import tarfile
archive, destination = sys.argv[1:]
with tarfile.open(archive) as source:
    for item in source.getmembers():
        path = pathlib.PurePosixPath(item.name)
        allowed = item.name == 'backend/app' or item.name.startswith('backend/app/') or item.name == 'frontend/dist' or item.name.startswith('frontend/dist/') or item.name == 'deploy/omnia' or item.name.startswith('deploy/omnia/')
        if not allowed or path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit(f'Nepovolená položka archivu: {item.name}')
    source.extractall(destination, filter='data')
root = pathlib.Path(destination)
for required in ['frontend/dist/index.html', 'backend/app/main.py', 'deploy/omnia/multicam.service', 'deploy/omnia/install-container.sh']:
    if not (root / required).is_file():
        raise SystemExit(f'Neúplný release: {required}')
for path in (root / 'backend/app').glob('*.py'):
    ast.parse(path.read_text())
PY
backup="/srv/multicam-ssd/app.previous-$(date +%Y%m%d-%H%M%S)-$$"
systemctl stop multicam
mv /srv/multicam-ssd/app "$backup"
if ! mv "$staging" /srv/multicam-ssd/app; then
  mv "$backup" /srv/multicam-ssd/app
  systemctl start multicam
  exit 1
fi
rollback() {
  systemctl stop multicam || true
  mv /srv/multicam-ssd/app "/srv/multicam-ssd/app.failed-$(date +%Y%m%d-%H%M%S)-$$"
  mv "$backup" /srv/multicam-ssd/app
  install -m 644 /srv/multicam-ssd/app/deploy/omnia/multicam.service /etc/systemd/system/multicam.service
  systemctl daemon-reload
  systemctl start multicam
  echo 'Aktualizace selhala; obnovena předchozí verze aplikace. Data a konfigurace zůstaly zachované.' >&2
  exit 1
}
cd /srv/multicam-ssd/app
sh deploy/omnia/install-container.sh || rollback
systemctl start multicam || rollback
if ! python3 - <<'PY'
import json
import ssl
import time
import urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('https://127.0.0.1:8000/api/health', context=ssl._create_unverified_context(), timeout=2) as response:
            if json.loads(response.read()).get('status') == 'ok':
                raise SystemExit(0)
    except (OSError, ValueError):
        pass
    time.sleep(1)
raise SystemExit(1)
PY
then rollback; fi
printf 'Aktualizace hotová. Předchozí aplikace: %s\n' "$backup"
