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

from .discovery import discovery


class Federation:
    def __init__(self) -> None:
        self.config_path = Path(os.getenv("MULTICAM_FEDERATION_CONFIG", "data/federation.json"))
        try:
            saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            saved = {}
        self.token = os.getenv("MULTICAM_FEDERATION_TOKEN", saved.get("token", ""))
        self.role = os.getenv("MULTICAM_FEDERATION_ROLE", saved.get("role", "standalone"))
        self.leader_url = os.getenv("MULTICAM_FEDERATION_LEADER_URL", saved.get("leader_url"))
        self.leader_backend_id = saved.get("leader_backend_id")
        self.backup_to_follower = os.getenv("MULTICAM_FEDERATION_BACKUP", "1" if saved.get("backup_to_follower") else "0") == "1"
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
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "token": self.token, "transfer_enabled": self.transfer_enabled, "tls_verify": self.tls_verify,
            "role": self.role, "leader_url": self.leader_url,
            "leader_backend_id": self.leader_backend_id,
            "backup_to_follower": self.backup_to_follower,
        }), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.config_path)

    def configure(
        self, *, token: str | None = None, transfer_enabled: bool | None = None,
        backup_to_follower: bool | None = None,
    ) -> None:
        if token is not None:
            if len(token) < 32:
                raise ValueError("Federation token must have at least 32 characters")
            self.token = token
        if transfer_enabled is not None:
            self.transfer_enabled = transfer_enabled
        if backup_to_follower is not None:
            self.backup_to_follower = backup_to_follower
        self.save()

    def create_pairing_offer(self) -> str:
        import time
        if not self.token:
            self.token = secrets.token_urlsafe(32)
        self.role = "leader"
        self.leader_url = discovery.advertised_url()
        self.leader_backend_id = discovery.backend_id
        self.save()
        # 10 characters from an unambiguous 32-character alphabet = 50 bits.
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        code = "".join(secrets.choice(alphabet) for _ in range(10))
        self._pairing_codes[code] = time.monotonic() + 300
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
        data = json.dumps({"code": code, "peer_url": discovery.advertised_url()}).encode()
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
        self.role = "follower"
        self.leader_url = response.get("leader_url", url).rstrip("/")
        self.leader_backend_id = response["leader_backend_id"]
        self.configure(token=response["token"])

    def mark_sync_ok(self) -> None:
        self.last_sync_at = datetime.now(timezone.utc).isoformat()
        self.last_error = None

    def mark_sync_error(self, error: Exception) -> None:
        self.last_error = str(error) or error.__class__.__name__

    async def pair_with_discovered_peer(self, code: str) -> None:
        results = await asyncio.gather(*(
            self.pair_with(peer["url"], code) for peer in discovery.snapshot()
        ), return_exceptions=True)
        if not results or all(isinstance(result, Exception) for result in results):
            raise ValueError("No discovered backend accepted the pairing code")

    def target_peers(self) -> list[dict]:
        peers = discovery.snapshot()
        if self.role == "follower" and self.leader_backend_id:
            matched = [peer for peer in peers if peer["backend_id"] == self.leader_backend_id]
            if not matched and self.leader_url:
                return [{"backend_id": self.leader_backend_id, "url": self.leader_url, "name": "leader"}]
            return matched
        return peers

    def _request(self, url: str, *, data: bytes | None = None, content_type: str = "application/json") -> bytes:
        request = urllib.request.Request(url, data=data, headers={
            "X-MultiCam-Federation": self.token, "Content-Type": content_type,
        }, method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=10, context=self._ssl_context()) as response:
            return response.read()

    async def get_snapshot(self, peer_url: str) -> dict[str, Any]:
        return json.loads(await asyncio.to_thread(self._request, f"{peer_url}/api/federation/snapshot"))

    async def post_json(self, peer_url: str, path: str, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        await asyncio.to_thread(self._request, f"{peer_url}{path}", data=data)

    async def broadcast_json(self, path: str, payload: dict) -> None:
        if self.enabled:
            await asyncio.gather(*(self.post_json(peer["url"], path, payload) for peer in self.target_peers()), return_exceptions=True)

    async def send_to_leader(self, path: str, payload: dict) -> None:
        if not self.enabled or self.role != "follower" or not self.leader_url:
            raise ValueError("Leader is not configured")
        await self.post_json(self.leader_url, path, payload)

    async def send_bundle(self, peer_url: str, path: Path, session_id: str, take_id: str) -> None:
        if not self.enabled or not self.transfer_enabled:
            return
        query = f"?session_id={session_id}&take_id={take_id}&source_backend_id={discovery.backend_id}"
        data = await asyncio.to_thread(path.read_bytes)
        await asyncio.to_thread(self._request, f"{peer_url}/api/federation/take{query}", data=data, content_type="application/zip")


federation = Federation()
