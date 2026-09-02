"""Authenticated backend-to-backend transport."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import ssl
import urllib.request
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
        configured_transfer = os.getenv("MULTICAM_FEDERATION_TRANSFER")
        self.transfer_enabled = configured_transfer != "0" if configured_transfer is not None else saved.get("transfer_enabled", True)
        self._pairing_codes: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _ssl_context(self) -> ssl.SSLContext:
        if ca := os.getenv("MULTICAM_FEDERATION_CA"):
            return ssl.create_default_context(cafile=ca)
        if os.getenv("MULTICAM_FEDERATION_TLS_VERIFY", "1") == "0":
            return ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
        return ssl.create_default_context()

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"token": self.token, "transfer_enabled": self.transfer_enabled}), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.config_path)

    def configure(self, *, token: str | None = None, transfer_enabled: bool | None = None) -> None:
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
        self._pairing_codes[code] = time.monotonic() + 300
        return code

    def accept_pairing(self, code: str) -> str:
        import time
        expires = self._pairing_codes.pop(code, 0)
        if expires < time.monotonic():
            raise ValueError("Pairing code is invalid or expired")
        return self.token

    async def pair_with(self, url: str, code: str) -> None:
        data = json.dumps({"code": code}).encode()
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/federation/pair/accept", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        context = ssl._create_unverified_context() if url.startswith("https://") else None  # noqa: SLF001
        def exchange() -> dict:
            with urllib.request.urlopen(request, timeout=10, context=context) as response:
                return json.loads(response.read())
        response = await asyncio.to_thread(exchange)
        self.configure(token=response["token"])

    async def pair_with_discovered_peer(self, code: str) -> None:
        results = await asyncio.gather(*(
            self.pair_with(peer["url"], code) for peer in discovery.snapshot()
        ), return_exceptions=True)
        if not results or all(isinstance(result, Exception) for result in results):
            raise ValueError("No discovered backend accepted the pairing code")

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
            await asyncio.gather(*(self.post_json(peer["url"], path, payload) for peer in discovery.snapshot()), return_exceptions=True)

    async def send_bundle(self, peer_url: str, path: Path, session_id: str, take_id: str) -> None:
        if not self.enabled or not self.transfer_enabled:
            return
        query = f"?session_id={session_id}&take_id={take_id}&source_backend_id={discovery.backend_id}"
        data = await asyncio.to_thread(path.read_bytes)
        await asyncio.to_thread(self._request, f"{peer_url}/api/federation/take{query}", data=data, content_type="application/zip")


federation = Federation()
