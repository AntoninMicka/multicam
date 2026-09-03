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
    assert peers[0]["url"] == "https://10.10.10.2:8000"
    assert service.diagnostics()["received_packets"] == 2


def test_discovery_expires_old_peers(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    service = BackendDiscovery()
    service.ttl = 1
    peer_id = str(uuid4())
    service.receive(json.dumps({"protocol": PROTOCOL, "backend_id": peer_id, "name": "old", "url": "http://192.0.2.1:8000"}).encode(), "192.0.2.1")
    service.peers[peer_id].last_seen = time.monotonic() - 2
    assert service.snapshot() == []


def test_discovery_advertises_zerotier_address_automatically(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    monkeypatch.delenv("MULTICAM_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MULTICAM_ADVERTISE_HOST", raising=False)
    monkeypatch.setattr("app.discovery.interface_addresses", lambda: [
        {"interface": "eth0", "family": "ipv4", "address": "192.168.1.4"},
        {"interface": "ztabc", "family": "ipv4", "address": "10.10.0.4"},
    ])
    assert BackendDiscovery().advertised_url() == "https://10.10.0.4:8000"


def test_discovery_refreshes_automatic_interface_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    monkeypatch.delenv("MULTICAM_DISCOVERY_INTERFACE_IP", raising=False)
    addresses = [{"interface": "eth0", "family": "ipv4", "address": "192.168.1.4"}]
    monkeypatch.setattr("app.discovery.interface_addresses", lambda: addresses)
    service = BackendDiscovery()
    assert service._current_multicast_ips() == ["192.168.1.4"]
    addresses.append({"interface": "ztabc", "family": "ipv4", "address": "10.10.0.4"})
    assert service._current_multicast_ips() == ["192.168.1.4", "10.10.0.4"]


def test_discovery_routes_pairing_code_without_exposing_it_in_snapshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    service = BackendDiscovery()
    peer_id = str(uuid4())
    service.receive(json.dumps({
        "protocol": PROTOCOL, "backend_id": peer_id, "name": "leader",
        "url": "https://leader:8000", "pairing_code": "23456ABCDE",
    }).encode(), "10.10.0.2")
    assert "pairing_code" not in service.snapshot()[0]
    assert service.peers_with_pairing_code("23456ABCDE")[0]["url"] == "https://10.10.0.2:8000"
