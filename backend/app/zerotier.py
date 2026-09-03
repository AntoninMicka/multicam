"""Narrow ZeroTier CLI adapter for the local director UI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

NETWORK_ID = re.compile(r"^[0-9a-fA-F]{16}$")


class ZeroTierError(RuntimeError):
    pass


def _run(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def status() -> dict:
    cli = shutil.which("zerotier-cli")
    if not cli:
        return {"installed": False, "online": False, "networks": [], "detail": "ZeroTier není nainstalovaný."}
    info = _run([cli, "-j", "info"])
    networks = _run([cli, "-j", "listnetworks"])
    if info.returncode or networks.returncode:
        detail = (info.stderr or networks.stderr or info.stdout or networks.stdout).strip()
        return {"installed": True, "online": False, "networks": [], "detail": detail or "ZeroTier CLI není dostupné pro tohoto uživatele."}
    try:
        node = json.loads(info.stdout)
        joined = json.loads(networks.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ZeroTierError("ZeroTier vrátil neplatný stav") from error
    return {
        "installed": True,
        "online": bool(node.get("online")),
        "node_id": node.get("address"),
        "version": node.get("version"),
        "networks": [{
            "id": item.get("nwid"), "name": item.get("name") or "nepojmenovaná síť",
            "status": item.get("status"), "type": item.get("type"),
            "interface": item.get("portDeviceName"),
            "addresses": item.get("assignedAddresses", []),
        } for item in joined],
        "detail": None,
    }


def join(network_id: str, project_dir: Path, install: bool = False) -> dict:
    if not network_id and not install:
        raise ZeroTierError("Pro připojení sítě je Network ID povinné")
    if network_id and not NETWORK_ID.fullmatch(network_id):
        raise ZeroTierError("Network ID musí obsahovat přesně 16 hexadecimálních znaků")
    normalized = network_id.lower()
    pkexec = shutil.which("pkexec")
    if not pkexec:
        suffix = f" {normalized}" if normalized else ""
        raise ZeroTierError(f"Chybí pkexec. Použijte v terminálu: sudo ./scripts/setup-zerotier.sh {'--install' if install else ''}{suffix}")
    cli = shutil.which("zerotier-cli")
    if install or not cli:
        command = [pkexec, str(project_dir / "scripts" / "setup-zerotier.sh"), "--install"]
        if normalized:
            command.append(normalized)
    else:
        command = [pkexec, cli, "join", normalized]
    result = _run(command, timeout=180)
    if result.returncode:
        raise ZeroTierError((result.stderr or result.stdout).strip() or "Připojení k ZeroTier síti selhalo")
    return {"accepted": True, "network_id": normalized or None, "detail": (result.stdout or "Požadavek byl odeslán.").strip()}
