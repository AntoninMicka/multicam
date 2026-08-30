import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.store import SessionStore, store
from app.uploads import UploadService, uploads


async def request(method: str, url: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path) -> None:
    root = tmp_path / "sessions"
    store._sessions.clear()
    store.root = root
    uploads.root = root


def test_health() -> None:
    assert asyncio.run(request("GET", "/api/health")).json() == {"status": "ok"}


def test_hotspot_status(tmp_path, monkeypatch) -> None:
    status_path = tmp_path / "hotspot.json"
    status_path.write_text('{"active":true,"ssid":"MultiCam","app_url":"https://10.42.0.1:8000/"}')
    monkeypatch.setenv("MULTICAM_HOTSPOT_STATUS", str(status_path))
    response = asyncio.run(request("GET", "/api/hotspot"))
    assert response.json()["ssid"] == "MultiCam"


def test_create_session_and_register_device() -> None:
    response = asyncio.run(request("POST", "/api/sessions", json={"name": "Test"}))
    assert response.status_code == 201
    session = response.json()
    assert session["schema_version"] == "1.0"

    response = asyncio.run(request(
        "POST",
        f"/api/sessions/{session['session_id']}/devices",
        json={"name": "Kamera 1", "role": "main_camera", "capabilities": {"battery_percent": 87}},
    ))
    assert response.status_code == 201
    assert response.json()["state"] == "ready"

    response = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}"))
    assert len(response.json()["devices"]) == 1

    response = asyncio.run(request("GET", "/api/sessions/current"))
    assert response.json()["session_id"] == session["session_id"]

    for number in (1, 2):
        response = asyncio.run(request(
            "POST",
            f"/api/sessions/{session['session_id']}/devices",
            json={"name": f"Vedlejší {number}", "role": "secondary_camera"},
        ))
        assert response.status_code == 201

    response = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}"))
    secondary = [device for device in response.json()["devices"].values() if device["role"] == "secondary_camera"]
    assert len(secondary) == 2


def test_unknown_session_is_404() -> None:
    response = asyncio.run(request("GET", "/api/sessions/00000000-0000-0000-0000-000000000000"))
    assert response.status_code == 404


def test_legacy_session_without_manifest_is_recovered(tmp_path) -> None:
    session_id = UUID("11111111-1111-4111-8111-111111111111")
    device_id = UUID("22222222-2222-4222-8222-222222222222")
    upload_id = UUID("33333333-3333-4333-8333-333333333333")
    root = tmp_path / "sessions"
    relative_video = Path(str(session_id)) / "devices" / str(device_id) / "recordings" / f"recording-{upload_id}.webm"
    video = root / relative_video
    video.parent.mkdir(parents=True)
    video.write_bytes(b"legacy video")
    metadata_path = root / str(session_id) / "devices" / str(device_id) / ".uploads" / str(upload_id) / "upload.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(json.dumps({
        "upload_id": str(upload_id),
        "file_name": "recording.webm",
        "mime_type": "video/webm",
        "size_bytes": video.stat().st_size,
        "complete": True,
        "receipt": {"file_path": str(relative_video)},
    }))

    recovered_store = SessionStore(root)
    recovered = asyncio.run(recovered_store.get(session_id))
    assert recovered.name.startswith("Obnovená relace")
    assert (root / str(session_id) / "session.json").is_file()
    media = UploadService(root).list_media(recovered)
    assert len(media) == 1
    assert media[0].telemetry_url is None


def test_chunked_upload_is_idempotent_and_verified(tmp_path) -> None:
    session = asyncio.run(request("POST", "/api/sessions", json={"name": "Upload test"})).json()
    device = asyncio.run(request(
        "POST",
        f"/api/sessions/{session['session_id']}/devices",
        json={"name": "Kamera upload", "role": "secondary_camera"},
    )).json()
    content = b"a" * (256 * 1024) + b"last chunk"
    digest = hashlib.sha256(content).hexdigest()
    base = f"/api/sessions/{session['session_id']}/devices/{device['device_id']}/uploads"
    upload = asyncio.run(request("POST", base, json={
        "file_name": "recording.webm",
        "mime_type": "video/webm",
        "size_bytes": len(content),
        "sha256": digest,
        "chunk_size": 256 * 1024,
        "total_chunks": 2,
    })).json()

    first = content[:256 * 1024]
    first_url = f"{base}/{upload['upload_id']}/chunks/0"
    bad = asyncio.run(request("PUT", first_url, content=first, headers={"X-Chunk-SHA256": "0" * 64}))
    assert bad.status_code == 409
    headers = {"X-Chunk-SHA256": hashlib.sha256(first).hexdigest()}
    assert asyncio.run(request("PUT", first_url, content=first, headers=headers)).status_code == 200
    assert asyncio.run(request("PUT", first_url, content=first, headers=headers)).status_code == 200

    last = content[256 * 1024:]
    last_url = f"{base}/{upload['upload_id']}/chunks/1"
    headers = {"X-Chunk-SHA256": hashlib.sha256(last).hexdigest()}
    assert asyncio.run(request("PUT", last_url, content=last, headers=headers)).status_code == 200

    complete_url = f"{base}/{upload['upload_id']}/complete"
    receipt = asyncio.run(request("POST", complete_url)).json()
    assert receipt["verified"] is True
    assert receipt["sha256"] == digest
    assert (uploads.root / receipt["file_path"]).read_bytes() == content
    assert asyncio.run(request("POST", complete_url)).json()["receipt_id"] == receipt["receipt_id"]

    telemetry = (
        b'{"schema_version":"1.0","event":"recording_started","monotonic_ms":1}\n'
        b'{"schema_version":"1.0","event":"sync_marker","details":{"requested_at":"2026-08-30T12:00:00Z"}}\n'
    )
    telemetry_digest = hashlib.sha256(telemetry).hexdigest()
    telemetry_upload = asyncio.run(request("POST", base, json={
        "capture_id": receipt["capture_id"],
        "kind": "telemetry",
        "file_name": "timing.jsonl",
        "mime_type": "application/x-ndjson",
        "size_bytes": len(telemetry),
        "sha256": telemetry_digest,
        "chunk_size": 256 * 1024,
        "total_chunks": 1,
    })).json()
    telemetry_url = f"{base}/{telemetry_upload['upload_id']}/chunks/0"
    headers = {"X-Chunk-SHA256": telemetry_digest}
    assert asyncio.run(request("PUT", telemetry_url, content=telemetry, headers=headers)).status_code == 200
    telemetry_receipt = asyncio.run(request("POST", f"{base}/{telemetry_upload['upload_id']}/complete")).json()
    assert telemetry_receipt["kind"] == "telemetry"
    assert (uploads.root / telemetry_receipt["file_path"]).read_bytes() == telemetry

    current_session = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}")).json()
    assert current_session["devices"][device["device_id"]]["state"] == "verified"

    media = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}/media")).json()
    assert len(media) == 1
    assert media[0]["capture_id"] == receipt["capture_id"]
    assert media[0]["take_id"] is not None
    report = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}/report")).json()
    assert report["takes"][0]["complete"] is True
    assert report["takes"][0]["streams"][0]["artifacts"]["recording"]["sha256"] == digest
    assert (uploads.root / session["session_id"] / "report.json").is_file()
    assert uploads.artifact_path(
        UUID(session["session_id"]), UUID(device["device_id"]), UUID(receipt["capture_id"]), "recording"
    ).read_bytes() == content
    telemetry_text = uploads.artifact_path(
        UUID(session["session_id"]), UUID(device["device_id"]), UUID(receipt["capture_id"]), "telemetry"
    ).read_text()
    assert '"event":"recording_started"' in telemetry_text
    assert (uploads.root / session["session_id"] / "session.json").is_file()
    restored = SessionStore(uploads.root)
    assert asyncio.run(restored.get(UUID(session["session_id"]))).name == "Upload test"
