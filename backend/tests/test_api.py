import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app, deleted_session_ids, upload_leases
from app.federation import federation
from app.discovery import discovery
from app.models import SessionState
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
    store.active_session_id = None
    store.active_changed_at = None
    store.active_backend_id = None
    uploads.root = root
    federation.config_path = tmp_path / "federation.json"
    federation.token = ""
    federation.transfer_enabled = True
    federation.tls_verify = True
    federation.director_backend_id = discovery.backend_id
    federation.storage_backend_id = discovery.backend_id
    federation.peers = {}
    federation.assignment_revision = 0
    from app.main import applied_controls
    applied_controls.clear()
    deleted_session_ids.clear()
    upload_leases.clear()


def test_health() -> None:
    assert asyncio.run(request("GET", "/api/health")).json() == {"status": "ok"}


def test_hotspot_status(tmp_path, monkeypatch) -> None:
    status_path = tmp_path / "hotspot.json"
    status_path.write_text('{"active":true,"ssid":"MultiCam","app_url":"https://10.42.0.1:8000/"}')
    monkeypatch.setenv("MULTICAM_HOTSPOT_STATUS", str(status_path))
    response = asyncio.run(request("GET", "/api/hotspot"))
    assert response.json()["ssid"] == "MultiCam"


def test_network_interfaces_are_exposed_for_frontend_qr(monkeypatch) -> None:
    monkeypatch.setattr("app.main.interface_addresses", lambda: [{
        "interface": "ztabc123", "family": "ipv4", "address": "10.10.0.2",
        "url": "https://10.10.0.2:8000/",
    }])
    response = asyncio.run(request("GET", "/api/network-interfaces"))
    assert response.status_code == 200
    assert response.json()["interfaces"][0]["interface"] == "ztabc123"


def test_zerotier_status_and_join_are_available_to_local_director(monkeypatch) -> None:
    monkeypatch.setattr("app.main.zerotier_status", lambda: {
        "installed": True, "online": True, "node_id": "abcdef1234", "networks": [],
    })
    monkeypatch.setattr("app.main.join_zerotier", lambda network_id, project_dir, install: {
        "accepted": True, "network_id": network_id, "detail": "OK",
    })
    status_response = asyncio.run(request("GET", "/api/zerotier"))
    assert status_response.json()["online"] is True
    joined = asyncio.run(request("POST", "/api/zerotier/join", json={
        "network_id": "8056c2e21c000001", "install": False,
    }))
    assert joined.status_code == 200
    assert joined.json()["accepted"] is True


def test_federation_control_requires_token_and_applies_immediately(monkeypatch) -> None:
    monkeypatch.setattr(federation, "token", "shared-secret")
    session = asyncio.run(request("POST", "/api/sessions", json={"name": "Federated"})).json()
    payload = {
        "session_id": session["session_id"],
        "director_backend_id": discovery.backend_id,
        "message": {"type": "control.arm", "payload": {"command_id": "cmd-1"}},
    }
    assert asyncio.run(request("POST", "/api/federation/control", json=payload)).status_code == 401
    accepted = asyncio.run(request(
        "POST", "/api/federation/control", json=payload,
        headers={"X-MultiCam-Federation": "shared-secret"},
    ))
    assert accepted.status_code == 200
    current = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}"))
    assert current.json()["state"] == "armed"


def test_pairing_offer_is_one_time_and_persists_config(monkeypatch) -> None:
    monkeypatch.setattr(federation, "token", "")
    offer = asyncio.run(request("POST", "/api/federation/pair/offer"))
    assert offer.status_code == 200
    assert federation.is_director
    assert len(offer.json()["pairing_code"]) == 10
    assert offer.json()["pairing_code"].isalnum()
    from urllib.parse import parse_qs, urlparse
    code = parse_qs(urlparse(offer.json()["pairing_uri"]).query)["code"][0]
    assert code == offer.json()["pairing_code"]
    peer_id = "11111111-1111-4111-8111-111111111111"
    accepted = asyncio.run(request("POST", "/api/federation/pair/accept", json={
        "code": code, "peer_backend_id": peer_id, "peer_url": "https://10.10.0.2:8000",
    }))
    assert len(accepted.json()["token"]) >= 32
    assert federation.tls_verify is False
    assert federation.peers[peer_id] == "https://10.10.0.2:8000"
    assert federation.config_path.is_file()
    assert asyncio.run(request("POST", "/api/federation/pair/accept", json={
        "code": code, "peer_backend_id": peer_id, "peer_url": "https://10.10.0.2:8000",
    })).status_code == 400


def test_invalid_peer_does_not_consume_pairing_offer(monkeypatch) -> None:
    monkeypatch.setattr(federation, "token", "")
    offer = asyncio.run(request("POST", "/api/federation/pair/offer")).json()
    invalid = asyncio.run(request("POST", "/api/federation/pair/accept", json={
        "code": offer["pairing_code"], "peer_backend_id": "not-a-uuid", "peer_url": "file:///tmp",
    }))
    assert invalid.status_code == 400
    peer_id = "11111111-1111-4111-8111-111111111111"
    accepted = asyncio.run(request("POST", "/api/federation/pair/accept", json={
        "code": offer["pairing_code"], "peer_backend_id": peer_id, "peer_url": "https://10.10.0.2:8000",
    }))
    assert accepted.status_code == 200


def test_peer_targets_only_paired_members(monkeypatch) -> None:
    paired_id = "11111111-1111-4111-8111-111111111111"
    stranger_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(federation, "peers", {paired_id: "https://stored-paired:8000"})
    monkeypatch.setattr("app.federation.discovery.snapshot", lambda: [
        {"backend_id": paired_id, "url": "https://live-paired:8000", "name": "paired"},
        {"backend_id": stranger_id, "url": "https://stranger:8000", "name": "stranger"},
    ])
    assert federation.target_peers() == [{
        "backend_id": paired_id, "url": "https://live-paired:8000", "name": "paired",
    }]


def test_peer_prefers_live_discovery_url_for_director(monkeypatch) -> None:
    leader_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(federation, "token", "x" * 32)
    monkeypatch.setattr(federation, "director_backend_id", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(federation, "director_backend_id", leader_id)
    monkeypatch.setattr(federation, "peers", {leader_id: "https://stale-hostname:8000"})
    monkeypatch.setattr("app.federation.discovery.snapshot", lambda: [{
        "backend_id": leader_id, "url": "https://10.10.0.1:8000", "name": "leader",
    }])
    called = {}

    async def fake_post(peer_url, path, payload):
        called["url"] = peer_url

    monkeypatch.setattr(federation, "post_json", fake_post)
    asyncio.run(federation.send_to_director("/test", {}))
    assert called["url"] == "https://10.10.0.1:8000"


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


def test_non_director_cannot_create_or_activate_session(monkeypatch) -> None:
    first = asyncio.run(request("POST", "/api/sessions", json={"name": "Leader session"})).json()
    monkeypatch.setattr(federation, "token", "x" * 32)
    monkeypatch.setattr(federation, "director_backend_id", "11111111-1111-4111-8111-111111111111")
    refused = asyncio.run(request("POST", "/api/sessions", json={"name": "Follower session"}))
    assert refused.status_code == 409
    refused = asyncio.run(request("POST", f"/api/sessions/{first['session_id']}/activate"))
    assert refused.status_code == 409


def test_deferred_federation_transfer_reports_queue_state(monkeypatch) -> None:
    monkeypatch.setattr(federation, "token", "x" * 32)
    monkeypatch.setattr(federation, "director_backend_id", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(federation, "transfer_enabled", False)
    monkeypatch.setattr(federation, "director_backend_id", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(federation, "peers", {federation.director_backend_id: "https://10.10.0.1:8000"})
    monkeypatch.setattr(federation, "storage_backend_id", federation.director_backend_id)
    response = asyncio.run(request("GET", "/api/federation/transfers"))
    assert response.status_code == 200
    assert response.json()["deferred"] is True
    assert response.json()["direction"] == "to_storage"


def test_upload_is_locked_during_recording() -> None:
    session = asyncio.run(request("POST", "/api/sessions", json={"name": "Záznam"})).json()
    device = asyncio.run(request(
        "POST", f"/api/sessions/{session['session_id']}/devices",
        json={"name": "Kamera", "role": "secondary_camera"},
    )).json()
    asyncio.run(store.set_state(UUID(session["session_id"]), SessionState.RECORDING))
    response = asyncio.run(request(
        "POST", f"/api/sessions/{session['session_id']}/devices/{device['device_id']}/uploads",
        json={
            "file_name": "recording.webm", "mime_type": "video/webm", "size_bytes": 1,
            "sha256": "0" * 64, "chunk_size": 256 * 1024, "total_chunks": 1,
        },
    ))
    assert response.status_code == 423


def test_session_can_be_deleted_but_not_while_recording(monkeypatch) -> None:
    monkeypatch.setattr(federation, "token", "")
    session = asyncio.run(request("POST", "/api/sessions", json={"name": "Ke smazání"})).json()
    assert asyncio.run(request("POST", f"/api/sessions/{session['session_id']}/close")).status_code == 200
    deleted = asyncio.run(request("DELETE", f"/api/sessions/{session['session_id']}"))
    assert deleted.status_code == 200
    assert asyncio.run(request("GET", f"/api/sessions/{session['session_id']}" )).status_code == 404
    assert not (uploads.root / session["session_id"]).exists()

    recording = asyncio.run(request("POST", "/api/sessions", json={"name": "Běží"})).json()
    asyncio.run(store.set_state(UUID(recording["session_id"]), SessionState.RECORDING))
    refused = asyncio.run(request("DELETE", f"/api/sessions/{recording['session_id']}"))
    assert refused.status_code == 409


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


def test_chunked_upload_is_idempotent_and_verified(tmp_path, monkeypatch, webm_bytes) -> None:
    session = asyncio.run(request("POST", "/api/sessions", json={"name": "Upload test"})).json()
    device = asyncio.run(request(
        "POST",
        f"/api/sessions/{session['session_id']}/devices",
        json={"name": "Kamera upload", "role": "secondary_camera"},
    )).json()
    content = webm_bytes
    chunk_size = max(256 * 1024, (len(content) + 1) // 2)
    assert len(content) > chunk_size
    digest = hashlib.sha256(content).hexdigest()
    base = f"/api/sessions/{session['session_id']}/devices/{device['device_id']}/uploads"
    upload = asyncio.run(request("POST", base, json={
        "file_name": "recording.webm",
        "mime_type": "video/webm",
        "size_bytes": len(content),
        "sha256": digest,
        "chunk_size": chunk_size,
        "total_chunks": 2,
    })).json()

    first = content[:chunk_size]
    first_url = f"{base}/{upload['upload_id']}/chunks/0"
    bad = asyncio.run(request("PUT", first_url, content=first, headers={"X-Chunk-SHA256": "0" * 64}))
    assert bad.status_code == 409
    headers = {"X-Chunk-SHA256": hashlib.sha256(first).hexdigest()}
    assert asyncio.run(request("PUT", first_url, content=first, headers=headers)).status_code == 200
    assert asyncio.run(request("PUT", first_url, content=first, headers=headers)).status_code == 200

    last = content[chunk_size:]
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
    assert media[0]["available_locally"] is True
    monkeypatch.setattr(federation, "token", "x" * 32)
    monkeypatch.setattr(federation, "director_backend_id", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(federation, "peers", {})
    follower_media = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}/media")).json()
    assert follower_media[0]["capture_id"] == receipt["capture_id"]
    assert follower_media[0]["available_locally"] is True
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

    deleted = asyncio.run(request(
        "DELETE", f"/api/sessions/{session['session_id']}/takes/{media[0]['take_id']}"
    ))
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 1
    assert asyncio.run(request("GET", f"/api/sessions/{session['session_id']}/media")).json() == []
    assert not (uploads.root / receipt["file_path"]).exists()
    assert not (uploads.root / telemetry_receipt["file_path"]).exists()


def test_closed_session_is_terminal_and_delete_is_local(monkeypatch):
    from app.main import merge_federation_snapshot
    session = asyncio.run(request('POST', '/api/sessions', json={'name': 'Final'})).json()
    session_id = session['session_id']
    assert asyncio.run(request('POST', '/api/sessions', json={'name': 'Other'})).status_code == 409
    assert asyncio.run(request('POST', f'/api/sessions/{session_id}/close')).status_code == 200
    assert asyncio.run(request('GET', '/api/sessions/current')).status_code == 404
    assert asyncio.run(request('POST', f'/api/sessions/{session_id}/activate')).status_code == 409
    assert asyncio.run(request('POST', f'/api/sessions/{session_id}/devices', json={'name': 'Late', 'role': 'secondary_camera'})).status_code == 409
    restored = SessionStore(uploads.root)
    with pytest.raises(Exception, match='current'):
        asyncio.run(restored.current())
    peer_id = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(federation, 'token', 'x' * 32)
    monkeypatch.setattr(federation, 'director_backend_id', peer_id)
    monkeypatch.setattr(federation, 'peers', {peer_id: 'https://peer'})
    calls = []
    async def broadcast(*args):
        calls.append(args)
    monkeypatch.setattr(federation, 'broadcast_json', broadcast)
    assert asyncio.run(request('DELETE', f'/api/sessions/{session_id}')).status_code == 200
    assert calls == []
    # A stale director still advertising the historical session cannot restore it.
    asyncio.run(merge_federation_snapshot({
        'backend_id': peer_id, 'sessions': [session], 'active_session': None,
        **federation.assignments(),
    }, {'backend_id': peer_id}))
    assert asyncio.run(request('GET', f'/api/sessions/{session_id}')).status_code == 404
    assert asyncio.run(request('POST', '/api/federation/delete-session', json={'session_id': session_id},
                               headers={'X-MultiCam-Federation': 'x' * 32})).status_code == 410


def test_role_handover_prepares_new_director_and_keeps_storage_independent(monkeypatch):
    from app.main import assign_backend_roles
    peer_id = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(federation, 'token', 'x' * 32)
    monkeypatch.setattr(federation, 'peers', {peer_id: 'https://peer'})
    calls = []
    async def post(url, path, payload):
        calls.append((path, payload))
    monkeypatch.setattr(federation, 'post_json', post)
    asyncio.run(assign_backend_roles({'director_backend_id': peer_id, 'storage_backend_id': discovery.backend_id}))
    assert not federation.is_director
    assert federation.is_storage
    assert [path for path, _ in calls] == ['/api/federation/prepare-director', '/api/federation/assignments']
    assert federation.assignment_revision == 1
    saved = json.loads(federation.config_path.read_text())
    assert saved['director_backend_id'] == peer_id
    assert saved['storage_backend_id'] == discovery.backend_id
    federation.adopt_assignments({'assignment_revision': 0, 'director_backend_id': discovery.backend_id,
                                 'storage_backend_id': peer_id})
    assert not federation.is_director


def test_handover_refused_during_recording_and_when_target_offline(monkeypatch):
    from app.main import assign_backend_roles
    from fastapi import HTTPException
    peer_id = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(federation, 'token', 'x' * 32)
    monkeypatch.setattr(federation, 'peers', {peer_id: 'https://peer'})
    session = asyncio.run(request('POST', '/api/sessions', json={'name': 'Busy'})).json()
    asyncio.run(store.set_state(UUID(session['session_id']), SessionState.RECORDING))
    with pytest.raises(HTTPException):
        asyncio.run(assign_backend_roles({'director_backend_id': peer_id}))
    assert federation.is_director
    asyncio.run(store.set_state(UUID(session['session_id']), SessionState.STOPPED))
    async def offline(*args):
        raise OSError('offline')
    monkeypatch.setattr(federation, 'post_json', offline)
    with pytest.raises(OSError):
        asyncio.run(assign_backend_roles({'director_backend_id': peer_id}))
    assert federation.is_director
    assert federation.assignment_revision == 0


def test_storage_target_works_without_discovery(monkeypatch):
    storage_id = '11111111-1111-4111-8111-111111111111'
    other_id = '22222222-2222-4222-8222-222222222222'
    monkeypatch.setattr(federation, 'storage_backend_id', storage_id)
    monkeypatch.setattr(federation, 'peers', {storage_id: 'https://ssd', other_id: 'https://camera'})
    monkeypatch.setattr(discovery, 'snapshot', lambda: [])
    assert [peer['backend_id'] for peer in federation.direct_transfer_peers()] == [storage_id]


def test_stale_snapshot_cannot_undo_control_or_closure(tmp_path):
    from app.models import SessionCreate
    local = SessionStore(tmp_path / 'isolated')
    session = asyncio.run(local.create(SessionCreate(name='Current')))
    stale = session.model_copy(deep=True)
    asyncio.run(local.set_state(session.session_id, SessionState.ARMED))
    merged = asyncio.run(local.merge_remote(stale, 'director', 'local', authoritative=True))
    assert merged.state == SessionState.ARMED
    asyncio.run(local.set_state(session.session_id, SessionState.CLOSED))
    stale.state_revision = 100
    merged = asyncio.run(local.merge_remote(stale, 'director', 'local', authoritative=True))
    assert merged.state == SessionState.CLOSED


def test_snapshot_replays_missed_control_once(monkeypatch):
    from app.main import merge_federation_snapshot
    from app.models import Session, SocketMessage
    peer_id = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(federation, 'token', 'x' * 32)
    monkeypatch.setattr(federation, 'director_backend_id', peer_id)
    monkeypatch.setattr(federation, 'peers', {peer_id: 'https://peer'})
    remote = Session(name='Remote', state=SessionState.RECORDING, state_revision=2,
                     last_control=SocketMessage(type='recording.start', payload={'take_id': str(UUID(int=8)), 'state_revision': 2}).model_dump())
    snapshot = {'backend_id': peer_id, 'sessions': [remote.model_dump(mode='json')],
                'active_session': {'session_id': str(remote.session_id), 'changed_at': remote.created_at.isoformat()},
                **federation.assignments()}
    asyncio.run(merge_federation_snapshot(snapshot, {'backend_id': peer_id}))
    asyncio.run(merge_federation_snapshot(snapshot, {'backend_id': peer_id}))
    events = (uploads.root / str(remote.session_id) / 'events.jsonl').read_text().splitlines()
    assert len(events) == 1
    assert json.loads(events[0])['take_id'] == str(UUID(int=8))


def test_invalid_recording_is_uploaded_but_never_verified(tmp_path):
    session = asyncio.run(request('POST', '/api/sessions', json={'name': 'Invalid media'})).json()
    device = asyncio.run(request('POST', f"/api/sessions/{session['session_id']}/devices", json={'name': 'Camera', 'role': 'secondary_camera'})).json()
    # A cluster without an EBML initialization header reproduces the reported corruption.
    content = bytes.fromhex('1f43b675') + b'broken-cluster'
    base = f"/api/sessions/{session['session_id']}/devices/{device['device_id']}/uploads"
    digest = hashlib.sha256(content).hexdigest()
    upload = asyncio.run(request('POST', base, json={'file_name': 'broken.webm', 'mime_type': 'video/webm',
                                                    'size_bytes': len(content), 'sha256': digest,
                                                    'chunk_size': 256 * 1024, 'total_chunks': 1})).json()
    assert asyncio.run(request('PUT', base + f"/{upload['upload_id']}/chunks/0", content=content,
                               headers={'X-Chunk-SHA256': digest})).is_success
    failed = asyncio.run(request('POST', base + f"/{upload['upload_id']}/complete"))
    assert failed.status_code == 422
    assert failed.json()['detail']['code'] == 'media_validation_failed'
    status = asyncio.run(request('GET', base + f"/{upload['upload_id']}")).json()
    assert status['state'] == 'failed'
    assert status['complete'] is False
    metadata_path = next(uploads.root.glob('*/devices/*/.uploads/*/upload.json'))
    metadata = json.loads(metadata_path.read_text())
    assert metadata['transport_verified'] is True
    assert not metadata['media_validation']['verified']
    assert (uploads.root / metadata['diagnostic_file_path']).read_bytes() == content
    assert asyncio.run(request('POST', base + f"/{upload['upload_id']}/complete")).status_code == 422


def test_ffprobe_rejects_wrong_container_audio_only_and_unavailable_probe(tmp_path, monkeypatch, webm_bytes):
    import subprocess
    from app.media_validation import validate_media, MediaValidationError
    source = tmp_path / 'valid.webm'
    source.write_bytes(webm_bytes)
    assert validate_media(source, 'video/webm')['streams'][0]['codec_type'] == 'video'
    with pytest.raises(MediaValidationError, match='Kontejner'):
        validate_media(source, 'video/mp4')
    audio = tmp_path / 'audio.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=0.1', '-c:a', 'libopus', str(audio)], check=True)
    with pytest.raises(MediaValidationError, match='video stream'):
        validate_media(audio, 'video/webm')
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('ffprobe')
    monkeypatch.setattr(subprocess, 'run', unavailable)
    with pytest.raises(MediaValidationError, match='FFprobe'):
        validate_media(source, 'video/webm')
