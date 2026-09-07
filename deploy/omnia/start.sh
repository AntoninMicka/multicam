#!/bin/sh
set -eu
: "${MULTICAM_REQUIRED_STORAGE_MOUNT:?SSD mount is required}"
: "${MULTICAM_STORAGE_ID:?SSD identity is required}"
: "${MULTICAM_CERT:?TLS certificate is required}"
: "${MULTICAM_KEY:?TLS key is required}"
python3 -m backend.app.storage_guard
exec python3 -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 \
  --workers 1 --loop asyncio --http h11 --ws websockets \
  --ssl-certfile "$MULTICAM_CERT" --ssl-keyfile "$MULTICAM_KEY"
