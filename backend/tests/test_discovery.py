import json
import time
from uuid import uuid4

from app.discovery import BackendDiscovery, PROTOCOL


def test_discovery_accepts_valid_peer_and_ignores_invalid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    service = BackendDiscovery()
    peer_id = str(uuid4())
    service.receive(json.dumps({"protocol": PROTOCOL, "backend_id": peer_id, "name": "Pult B", "url": "https://10.10.10.2:8000"}).encode(), "10.10.10.2")
    service.receive(b"not json", "10.10.10.3")
    peers = service.snapshot()
    assert len(peers) == 1
    assert peers[0]["backend_id"] == peer_id
    assert peers[0]["address"] == "10.10.10.2"


def test_discovery_expires_old_peers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    service = BackendDiscovery()
    service.ttl = 1
    peer_id = str(uuid4())
    service.receive(json.dumps({"protocol": PROTOCOL, "backend_id": peer_id, "name": "old", "url": "http://192.0.2.1:8000"}).encode(), "192.0.2.1")
    service.peers[peer_id].last_seen = time.monotonic() - 2
    assert service.snapshot() == []
