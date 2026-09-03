"""Dependency-free discovery for MultiCam control servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from .network import interface_addresses

PROTOCOL = "multicam-discovery-v1"
logger = logging.getLogger(__name__)


@dataclass
class Peer:
    backend_id: str
    name: str
    url: str
    address: str
    last_seen: float
    pairing_code: str | None = None


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, discovery: "BackendDiscovery") -> None:
        self.discovery = discovery

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.discovery.receive(data, addr[0])


class BackendDiscovery:
    def __init__(self) -> None:
        self.enabled = os.getenv("MULTICAM_DISCOVERY", "1") != "0"
        self.group = os.getenv("MULTICAM_DISCOVERY_GROUP", "239.255.77.77")
        self.interface_ip = os.getenv("MULTICAM_DISCOVERY_INTERFACE_IP", "0.0.0.0")
        self.port = int(os.getenv("MULTICAM_DISCOVERY_PORT", "47777"))
        self.interval = float(os.getenv("MULTICAM_DISCOVERY_INTERVAL", "3"))
        self.ttl = float(os.getenv("MULTICAM_DISCOVERY_TTL", "12"))
        self.name = os.getenv("MULTICAM_BACKEND_NAME", socket.gethostname())
        self.backend_id = self._backend_id()
        self.peers: dict[str, Peer] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.task: asyncio.Task | None = None
        self.multicast_interface_ips: list[str] = []
        self.pairing_code: str | None = None
        self.pairing_expires_at: float = 0
        self.received_packets = 0
        self.last_received_at: float | None = None
        self.last_source: str | None = None
        self.last_rejection: str | None = None

    def _current_multicast_ips(self) -> list[str]:
        if self.interface_ip != "0.0.0.0":
            return [self.interface_ip]
        candidates = [
            item["address"] for item in interface_addresses()
            if item["family"] == "ipv4"
        ]
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _backend_id() -> str:
        configured = os.getenv("MULTICAM_BACKEND_ID")
        if configured:
            return str(UUID(configured))
        path = Path(os.getenv("MULTICAM_BACKEND_ID_FILE", "data/backend-id"))
        try:
            if path.is_file():
                return str(UUID(path.read_text(encoding="ascii").strip()))
            value = str(uuid4())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n", encoding="ascii")
            return value
        except OSError:
            return str(uuid4())

    def advertised_url(self) -> str:
        if explicit := os.getenv("MULTICAM_PUBLIC_URL"):
            return explicit.rstrip("/")
        scheme = "https" if os.getenv("MULTICAM_HTTPS", "1") != "0" else "http"
        host = os.getenv("MULTICAM_ADVERTISE_HOST")
        if not host:
            zerotier = next((
                item["address"] for item in interface_addresses()
                if item["family"] == "ipv4" and item["interface"].startswith("zt")
            ), None)
            host = zerotier or socket.gethostname()
        return f"{scheme}://{host}:{os.getenv('MULTICAM_PORT', '8000')}"

    def receive(self, raw: bytes, address: str) -> None:
        self.received_packets += 1
        self.last_received_at = time.monotonic()
        self.last_source = address
        try:
            message = json.loads(raw)
            peer_id = str(UUID(message["backend_id"]))
            if message.get("protocol") != PROTOCOL:
                self.last_rejection = "jiná verze discovery protokolu"
                return
            if peer_id == self.backend_id:
                self.last_rejection = "druhý pult používá stejné backend ID"
                return
            name, url = str(message["name"]), str(message["url"])
            pairing_code = message.get("pairing_code")
            if pairing_code is not None:
                pairing_code = str(pairing_code)
                if len(pairing_code) != 10 or not pairing_code.isalnum():
                    pairing_code = None
            if not name or not url.startswith(("http://", "https://")):
                self.last_rejection = "neplatný název nebo URL pultu"
                return
            # The address observed on the UDP packet is routable on the exact
            # interface that delivered discovery. Prefer it over advertised
            # hostnames, which commonly exist only in the peer's local DNS.
            parsed = urlsplit(url)
            port = parsed.port
            host = f"[{address}]" if ":" in address else address
            netloc = f"{host}:{port}" if port else host
            url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, "")).rstrip("/")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.last_rejection = "neplatný discovery paket"
            return
        self.peers[peer_id] = Peer(peer_id, name[:80], url, address, time.monotonic(), pairing_code)
        self.last_rejection = None

    def diagnostics(self) -> dict:
        age = time.monotonic() - self.last_received_at if self.last_received_at is not None else None
        return {
            "backend_id": self.backend_id,
            "received_packets": self.received_packets,
            "last_packet_seconds_ago": round(age, 1) if age is not None else None,
            "last_source": self.last_source,
            "last_rejection": self.last_rejection,
            "listening_interface_ips": self.multicast_interface_ips,
            "advertised_url": self.advertised_url(),
        }

    async def application_ping(self) -> list[dict]:
        targets = {peer.url for peer in self.peers.values()}
        if self.last_source:
            scheme = "https" if os.getenv("MULTICAM_HTTPS", "1") != "0" else "http"
            targets.add(f"{scheme}://{self.last_source}:{os.getenv('MULTICAM_PORT', '8000')}")

        def probe(base_url: str) -> dict:
            started = time.monotonic()
            try:
                context = ssl._create_unverified_context() if base_url.startswith("https://") else None  # noqa: SLF001
                with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/health", timeout=3, context=context) as response:
                    body = json.loads(response.read())
                    healthy = response.status == 200 and body.get("status") == "ok"
                return {
                    "url": base_url, "ok": healthy,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "detail": "MultiCam odpověděl" if healthy else "Neplatná odpověď aplikace",
                }
            except Exception as error:
                return {
                    "url": base_url, "ok": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "detail": str(error) or error.__class__.__name__,
                }

        return await asyncio.gather(*(asyncio.to_thread(probe, target) for target in sorted(targets)))

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        self.peers = {key: peer for key, peer in self.peers.items() if now - peer.last_seen <= self.ttl}
        return [{
            **{key: value for key, value in asdict(peer).items() if key != "pairing_code"},
            "last_seen_seconds_ago": round(now - peer.last_seen, 1),
        } for peer in self.peers.values()]

    def peers_with_pairing_code(self, code: str) -> list[dict]:
        self.snapshot()
        now = time.monotonic()
        return [{
            **{key: value for key, value in asdict(peer).items() if key != "pairing_code"},
            "last_seen_seconds_ago": round(now - peer.last_seen, 1),
        } for peer in self.peers.values() if peer.pairing_code == code]

    def advertise_pairing_code(self, code: str, expires_at: float) -> None:
        self.pairing_code = code
        self.pairing_expires_at = expires_at

    async def start(self) -> None:
        if not self.enabled or self.task:
            return
        loop = asyncio.get_running_loop()
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            socket.inet_aton(self.interface_ip)  # validate before starting the task
            sock.bind(("", self.port))
            self.multicast_interface_ips = self._current_multicast_ips()
            memberships = self.multicast_interface_ips or ["0.0.0.0"]
            joined = 0
            for interface_ip in memberships:
                try:
                    membership = socket.inet_aton(self.group) + socket.inet_aton(interface_ip)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    joined += 1
                except OSError as error:
                    logger.warning("Discovery multicast is unavailable on %s: %s", interface_ip, error)
            if not joined:
                raise OSError("No interface accepted the discovery multicast group")
            sock.setblocking(False)
            self.transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        except OSError as error:
            if sock is not None:
                sock.close()
            logger.warning("Backend discovery is unavailable: %s", error)
            return
        self.task = asyncio.create_task(self._announce_loop())

    async def _announce_loop(self) -> None:
        unicast_targets = [
            (host.strip(), self.port)
            for host in os.getenv("MULTICAM_DISCOVERY_PEERS", "").split(",")
            if host.strip()
        ]
        while True:
            if self.transport:
                raw_socket = self.transport.get_extra_info("socket")
                current_ips = self._current_multicast_ips()
                previous = set(self.multicast_interface_ips)
                current = set(current_ips)
                for interface_ip in current - previous:
                    try:
                        membership = socket.inet_aton(self.group) + socket.inet_aton(interface_ip)
                        raw_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                        logger.info("Discovery added interface %s", interface_ip)
                    except OSError as error:
                        logger.warning("Discovery could not add interface %s: %s", interface_ip, error)
                for interface_ip in previous - current:
                    try:
                        membership = socket.inet_aton(self.group) + socket.inet_aton(interface_ip)
                        raw_socket.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
                    except OSError:
                        pass
                self.multicast_interface_ips = current_ips
                active_pairing_code = self.pairing_code if time.monotonic() < self.pairing_expires_at else None
                payload = json.dumps({
                    "protocol": PROTOCOL, "backend_id": self.backend_id,
                    "name": self.name, "url": self.advertised_url(),
                    **({"pairing_code": active_pairing_code} if active_pairing_code else {}),
                }, separators=(",", ":")).encode()
                multicast_ips = self.multicast_interface_ips or ["0.0.0.0"]
                for interface_ip in multicast_ips:
                    try:
                        raw_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
                        self.transport.sendto(payload, (self.group, self.port))
                    except OSError as error:
                        logger.debug("Discovery announce failed on %s: %s", interface_ip, error)
                for target in unicast_targets:
                    self.transport.sendto(payload, target)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.transport:
            self.transport.close()
            self.transport = None


discovery = BackendDiscovery()
