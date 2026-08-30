import asyncio
import hashlib

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.uploads import uploads


async def request(method: str, url: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


def test_health() -> None:
    assert asyncio.run(request("GET", "/api/health")).json() == {"status": "ok"}


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


def test_chunked_upload_is_idempotent_and_verified(tmp_path) -> None:
    uploads.root = tmp_path
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
    assert (tmp_path / receipt["file_path"]).read_bytes() == content
    assert asyncio.run(request("POST", complete_url)).json()["receipt_id"] == receipt["receipt_id"]

    telemetry = b'{"schema_version":"1.0","event":"recording_started","monotonic_ms":1}\n'
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
    assert (tmp_path / telemetry_receipt["file_path"]).read_bytes() == telemetry

    current_session = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}")).json()
    assert current_session["devices"][device["device_id"]]["state"] == "verified"
