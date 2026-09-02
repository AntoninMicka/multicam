"""Dependency-free discovery for MultiCam control servers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

PROTOCOL = "multicam-discovery-v1"
logger = logging.getLogger(__name__)


@dataclass
class Peer:
    backend_id: str
    name: str
    url: str
    address: str
    last_seen: float


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
        host = os.getenv("MULTICAM_ADVERTISE_HOST", socket.gethostname())
        return f"{scheme}://{host}:{os.getenv('MULTICAM_PORT', '8000')}"

    def receive(self, raw: bytes, address: str) -> None:
        try:
            message = json.loads(raw)
            peer_id = str(UUID(message["backend_id"]))
            if message.get("protocol") != PROTOCOL or peer_id == self.backend_id:
                return
            name, url = str(message["name"]), str(message["url"])
            if not name or not url.startswith(("http://", "https://")):
                return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return
        self.peers[peer_id] = Peer(peer_id, name[:80], url, address, time.monotonic())

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        self.peers = {key: peer for key, peer in self.peers.items() if now - peer.last_seen <= self.ttl}
        return [{**asdict(peer), "last_seen_seconds_ago": round(now - peer.last_seen, 1)} for peer in self.peers.values()]

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
            membership = socket.inet_aton(self.group) + socket.inet_aton(self.interface_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            if self.interface_ip != "0.0.0.0":
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip))
            sock.setblocking(False)
            self.transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        except OSError as error:
            if sock is not None:
                sock.close()
            logger.warning("Backend discovery is unavailable: %s", error)
            return
        self.task = asyncio.create_task(self._announce_loop())

    async def _announce_loop(self) -> None:
        targets = [(self.group, self.port)]
        targets.extend((host.strip(), self.port) for host in os.getenv("MULTICAM_DISCOVERY_PEERS", "").split(",") if host.strip())
        payload = json.dumps({"protocol": PROTOCOL, "backend_id": self.backend_id, "name": self.name, "url": self.advertised_url()}, separators=(",", ":")).encode()
        while True:
            for target in targets:
                if self.transport:
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
