#!/bin/sh
# Run as root inside the Debian 13 LXC after unpacking the release onto SSD.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Spusťte jako root uvnitř LXC.' >&2; exit 1; }
. /etc/os-release
[ "$ID" = debian ] && [ "$VERSION_ID" = 13 ] || { echo 'Vyžadován Debian 13 LXC.' >&2; exit 1; }
[ -f /etc/multicam.env ] || { echo 'Nejprve vyplňte /etc/multicam.env.' >&2; exit 1; }
set -a
. /etc/multicam.env
set +a
cd /srv/multicam-ssd/app
python3 -m backend.app.storage_guard
[ -f frontend/dist/index.html ] || { echo 'Chybí sestavený frontend.' >&2; exit 1; }
apt-get update
apt-get install -y python3-fastapi python3-pydantic python3-uvicorn python3-websockets ffmpeg ca-certificates
python3 -c 'from pydantic import BaseModel; assert hasattr(BaseModel, "model_validate")'
id multicam >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin multicam
mkdir -p /srv/multicam-ssd/data /srv/multicam-ssd/tmp
chown multicam:multicam /srv/multicam-ssd /srv/multicam-ssd/data /srv/multicam-ssd/tmp
chgrp multicam "$(dirname "$MULTICAM_KEY")" "$MULTICAM_CERT" "$MULTICAM_KEY"
chmod 750 "$(dirname "$MULTICAM_KEY")"
chmod 640 "$MULTICAM_CERT" "$MULTICAM_KEY"
chmod 600 /etc/multicam.env
install -m 644 deploy/omnia/multicam.service /etc/systemd/system/multicam.service
systemctl daemon-reload
printf 'Připraveno. Spuštění: systemctl enable --now multicam\n'
