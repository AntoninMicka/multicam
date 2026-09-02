"""Authenticated backend-to-backend transport."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any

from .discovery import discovery


class Federation:
    def __init__(self) -> None:
        self.token = os.getenv("MULTICAM_FEDERATION_TOKEN", "")
        self.transfer_enabled = os.getenv("MULTICAM_FEDERATION_TRANSFER", "1") != "0"

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _ssl_context(self) -> ssl.SSLContext:
        if ca := os.getenv("MULTICAM_FEDERATION_CA"):
            return ssl.create_default_context(cafile=ca)
        if os.getenv("MULTICAM_FEDERATION_TLS_VERIFY", "1") == "0":
            return ssl._create_unverified_context()  # noqa: SLF001 - explicit operator choice
        return ssl.create_default_context()

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
