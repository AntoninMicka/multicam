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


def test_own_multicast_loopback_is_not_reported_as_id_collision(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MULTICAM_BACKEND_ID_FILE", str(tmp_path / "id"))
    service = BackendDiscovery()
    monkeypatch.setattr("app.discovery.interface_addresses", lambda: [{
        "interface": "ztabc", "family": "ipv4", "address": "10.10.0.1",
    }])
    payload = json.dumps({
        "protocol": PROTOCOL, "backend_id": service.backend_id,
        "name": "self", "url": "https://10.10.0.1:8000",
    }).encode()
    service.receive(payload, "10.10.0.1")
    assert service.diagnostics()["last_rejection"] is None
    service.receive(payload, "10.10.0.2")
    assert service.diagnostics()["last_rejection"] == "jiný pult používá stejné backend ID"


def test_memberships_retry_failed_interfaces_and_remove_disconnected(monkeypatch, tmp_path):
    import socket
    monkeypatch.setenv('MULTICAM_BACKEND_ID_FILE', str(tmp_path / 'id'))
    service = BackendDiscovery()
    addresses = ['192.168.1.4', '10.10.0.4']
    monkeypatch.setattr(service, '_current_multicast_ips', lambda: addresses)
    calls = []
    failed = {'10.10.0.4'}
    class Sock:
        def setsockopt(self, level, option, membership):
            ip = socket.inet_ntoa(membership[4:])
            calls.append((option, ip))
            if ip in failed:
                raise OSError('temporarily down')
    sock = Sock()
    service.refresh_memberships(sock)
    assert service.multicast_interface_ips == ['192.168.1.4']
    failed.clear()
    service.refresh_memberships(sock)
    assert set(service.multicast_interface_ips) == set(addresses)
    addresses.pop(0)
    service.refresh_memberships(sock)
    assert service.multicast_interface_ips == ['10.10.0.4']
    assert (socket.IP_DROP_MEMBERSHIP, '192.168.1.4') in calls


def test_preserves_routes_received_on_different_interfaces(monkeypatch, tmp_path):
    monkeypatch.setenv('MULTICAM_BACKEND_ID_FILE', str(tmp_path / 'id'))
    service = BackendDiscovery()
    peer_id = str(uuid4())
    packet = json.dumps({'protocol': PROTOCOL, 'backend_id': peer_id, 'name': 'Peer', 'url': 'https://peer:8000'}).encode()
    service.receive(packet, '192.168.1.2')
    service.receive(packet, '10.10.0.2')
    assert set(service.snapshot()[0]['urls']) == {'https://192.168.1.2:8000', 'https://10.10.0.2:8000'}
