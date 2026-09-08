"""Authenticated backend-to-backend transport."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage_guard import check_storage

from .discovery import discovery


class Federation:
    def __init__(self) -> None:
        self.config_path = Path(os.getenv("MULTICAM_FEDERATION_CONFIG", "data/federation.json"))
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            saved = {}
        self.token = os.getenv("MULTICAM_FEDERATION_TOKEN", saved.get("token", ""))
        legacy_role = saved.get("role", "standalone")
        self.peers: dict[str, str] = dict(saved.get("peers", saved.get("followers", {})))
        legacy_director = saved.get("leader_backend_id")
        if legacy_director and saved.get("leader_url") and legacy_director != discovery.backend_id:
            self.peers[legacy_director] = saved["leader_url"]
        self.director_backend_id = saved.get("director_backend_id") or (
            legacy_director if legacy_role == "follower" else discovery.backend_id
        )
        self.storage_backend_id = saved.get("storage_backend_id") or self.director_backend_id
        self.assignment_revision = int(saved.get("assignment_revision", 0))
        configured_transfer = os.getenv("MULTICAM_FEDERATION_TRANSFER")
        self.transfer_enabled = configured_transfer != "0" if configured_transfer is not None else saved.get("transfer_enabled", True)
        configured_verify = os.getenv("MULTICAM_FEDERATION_TLS_VERIFY")
        if configured_verify is not None:
            self.tls_verify = configured_verify != "0"
        elif "tls_verify" in saved:
            self.tls_verify = bool(saved["tls_verify"])
        else:
            # Configs created by the earlier QR handshake already authenticated
            # the peer but did not persist this flag; migrate them automatically.
            self.tls_verify = not bool(saved.get("token"))
        self._working_urls: dict[str, str] = {}
        self._pairing_codes: dict[str, float] = {}
        self.last_sync_at: str | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _ssl_context(self) -> ssl.SSLContext:
        if ca := os.getenv("MULTICAM_FEDERATION_CA"):
            return ssl.create_default_context(cafile=ca)
        if not self.tls_verify:
            return ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
        return ssl.create_default_context()

    def save(self) -> None:
        check_storage()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "token": self.token, "transfer_enabled": self.transfer_enabled, "tls_verify": self.tls_verify,
            **self.assignments(), "peers": self.peers,
        }), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.config_path)

    def configure(
        self, *, token: str | None = None, transfer_enabled: bool | None = None,
    ) -> None:
        if token is not None:
            if len(token) < 32:
                raise ValueError("Federation token must have at least 32 characters")
            self.token = token
        if transfer_enabled is not None:
            self.transfer_enabled = transfer_enabled
        self.save()

    def create_pairing_offer(self) -> str:
        import time
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        self.save()
        # 10 characters from an unambiguous 32-character alphabet = 50 bits.
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        code = "".join(secrets.choice(alphabet) for _ in range(10))
        expires_at = time.monotonic() + 300
        self._pairing_codes[code] = expires_at
        discovery.advertise_pairing_code(code, expires_at)
        return code

    def accept_pairing(self, code: str) -> str:
        import time
        expires = self._pairing_codes.pop(code, 0)
        if expires < time.monotonic():
            raise ValueError("Pairing code is invalid or expired")
        self.tls_verify = False
        self.save()
        return self.token

    async def pair_with(self, url: str, code: str) -> None:
        data = json.dumps({
            "code": code, "peer_url": discovery.advertised_url(),
            "peer_backend_id": discovery.backend_id,
        }).encode()
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/federation/pair/accept", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        context = ssl._create_unverified_context() if url.startswith("https://") else None  # noqa: SLF001
        def exchange() -> dict:
            with urllib.request.urlopen(request, timeout=10, context=context) as response:
                return json.loads(response.read())
        response = await asyncio.to_thread(exchange)
        self.tls_verify = False
        self.peers = response["peers"]
        self.peers.pop(discovery.backend_id, None)
        self.director_backend_id = response["director_backend_id"]
        self.storage_backend_id = response["storage_backend_id"]
        self.assignment_revision = response["assignment_revision"]
        self.configure(token=response["token"])

    def mark_sync_ok(self) -> None:
        self.last_sync_at = datetime.now(timezone.utc).isoformat()
        self.last_error = None

    def mark_sync_error(self, error: Exception) -> None:
        self.last_error = str(error) or error.__class__.__name__

    async def pair_with_discovered_peer(self, code: str) -> None:
        candidates = discovery.peers_with_pairing_code(code) or discovery.snapshot()
        for peer in candidates:
            try:
                await self.pair_with(peer["url"], code)
                return
            except (OSError, ValueError):
                continue
        raise ValueError("No discovered backend accepted the pairing code")

    @property
    def is_director(self) -> bool:
        return not self.enabled or self.director_backend_id == discovery.backend_id

    @property
    def is_storage(self) -> bool:
        return not self.enabled or self.storage_backend_id == discovery.backend_id

    def assignments(self) -> dict:
        return {"director_backend_id": self.director_backend_id,
                "storage_backend_id": self.storage_backend_id,
                "assignment_revision": self.assignment_revision}

    def membership(self) -> dict[str, str]:
        return {**self.peers, discovery.backend_id: discovery.advertised_url()}

    def register_peer(self, backend_id: str, url: str) -> None:
        from uuid import UUID
        backend_id = str(UUID(backend_id))
        if not url.startswith(("http://", "https://")):
            raise ValueError("Peer URL must use HTTP or HTTPS")
        if backend_id != discovery.backend_id and self.peers.get(backend_id) != url.rstrip("/"):
            self.peers[backend_id] = url.rstrip("/")
            self.save()

    def adopt_assignments(self, data: dict) -> None:
        revision = int(data["assignment_revision"])
        if revision <= self.assignment_revision:
            return
        members = self.membership()
        if data["director_backend_id"] not in members or data["storage_backend_id"] not in members:
            raise ValueError("Assigned backend must be paired")
        self.director_backend_id = data["director_backend_id"]
        self.storage_backend_id = data["storage_backend_id"]
        self.assignment_revision = revision
        self.save()

    def target_peers(self) -> list[dict]:
        # Discovery may refresh URLs only for explicitly trusted members.
        live = {peer["backend_id"]: peer for peer in discovery.snapshot()}
        return [live.get(backend_id, {"backend_id": backend_id, "url": url, "name": backend_id})
                for backend_id, url in self.peers.items() if backend_id != discovery.backend_id]

    def direct_transfer_peers(self) -> list[dict]:
        # A configured unicast address works without multicast (e.g. on Omnia).
        return [peer for peer in self.target_peers() if peer["backend_id"] == self.storage_backend_id]

    async def send_to_director(self, path: str, payload: dict) -> None:
        peer = next((p for p in self.target_peers() if p["backend_id"] == self.director_backend_id), None)
        if not self.enabled or not peer:
            raise ValueError("Director is not available")
        await self.post_json(peer["url"], path, payload)

    def _request(self, url: str, *, data: bytes | None = None, content_type: str = "application/json") -> bytes:
        request = urllib.request.Request(url, data=data, headers={
            "X-MultiCam-Federation": self.token, "Content-Type": content_type,
        }, method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=10, context=self._ssl_context()) as response:
            return response.read()

    async def request_peer(self, peer_url: str, path: str, *, data: bytes | None = None) -> bytes:
        peer = next((p for p in self.target_peers() if p["url"] == peer_url), None)
        candidates = [peer_url]
        if peer:
            candidates = [self._working_urls.get(peer["backend_id"], peer_url), peer_url,
                          *peer.get("urls", []), self.peers[peer["backend_id"]]]
        last_error = None
        for candidate in dict.fromkeys(candidates):
            try:
                result = await asyncio.to_thread(self._request, f"{candidate}{path}", data=data)
                if peer:
                    self._working_urls[peer["backend_id"]] = candidate
                return result
            except urllib.error.HTTPError:
                raise  # An application rejection is not a routing problem.
            except OSError as error:
                last_error = error
        raise last_error or OSError("No reachable peer interface")

    async def get_snapshot(self, peer_url: str) -> dict[str, Any]:
        return json.loads(await self.request_peer(peer_url, "/api/federation/snapshot"))

    async def get_json(self, peer_url: str, path: str) -> Any:
        return json.loads(await self.request_peer(peer_url, path))

    async def post_json(self, peer_url: str, path: str, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        await self.request_peer(peer_url, path, data=data)

    async def broadcast_json(self, path: str, payload: dict) -> None:
        if not self.enabled:
            return
        peers = self.target_peers()
        if not peers:
            error = ValueError("No federated peer is registered")
            self.mark_sync_error(error)
            return
        results = await asyncio.gather(*(self.post_json(peer["url"], path, payload) for peer in peers), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if len(failures) == len(results):
            self.mark_sync_error(failures[0])

    async def send_bundle(self, peer_url: str, path: Path, session_id: str, take_id: str, *, force: bool = False) -> None:
        if not self.enabled or not (self.transfer_enabled or force):
            raise ValueError("Přenos je vypnutý; data nebyla odeslána")
        target = next((p for p in self.target_peers() if p["url"] == peer_url), None)
        if target:
            peer_url = self._working_urls.get(target["backend_id"], peer_url)
        query = f"?session_id={session_id}&take_id={take_id}&source_backend_id={discovery.backend_id}"
        def transfer() -> None:
            request = urllib.request.Request(
                f"{peer_url}/api/federation/take{query}",
                headers={"X-MultiCam-Federation": self.token,
                         "Content-Type": "application/zip", "Content-Length": str(path.stat().st_size)},
                method="POST",
            )
            with path.open("rb") as source:
                request.data = source
                request.add_header("Content-Length", str(path.stat().st_size))
                with urllib.request.urlopen(request, timeout=300, context=self._ssl_context()) as response:
                    result = json.loads(response.read())
                    if result.get("verified") is not True:
                        raise ValueError("Storage did not verify the transfer")
        await asyncio.to_thread(transfer)


federation = Federation()
