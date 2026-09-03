"""Narrow ZeroTier CLI adapter for the local director UI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .network import interface_addresses

NETWORK_ID = re.compile(r"^[0-9a-fA-F]{16}$")
EXECUTABLE_DIRS = ("/usr/sbin", "/usr/local/sbin", "/usr/bin", "/usr/local/bin", "/sbin")


class ZeroTierError(RuntimeError):
    pass


def _config_path() -> Path:
    return Path(os.getenv("MULTICAM_ZEROTIER_CONFIG", "data/zerotier.json"))


def remembered_network_id() -> str | None:
    configured = os.getenv("MULTICAM_ZEROTIER_NETWORK", "").strip().lower()
    if NETWORK_ID.fullmatch(configured):
        return configured
    try:
        saved = json.loads(_config_path().read_text(encoding="utf-8"))
        value = str(saved.get("network_id", "")).strip().lower()
        return value if NETWORK_ID.fullmatch(value) else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _remember_network_id(network_id: str) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"network_id": network_id}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in EXECUTABLE_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def _run(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _visible_networks(network_id: str | None) -> list[dict]:
    """Describe ZeroTier interfaces without access to the daemon auth token."""
    grouped: dict[str, list[str]] = {}
    for item in interface_addresses():
        if item["interface"].startswith("zt"):
            grouped.setdefault(item["interface"], []).append(item["address"])
    # An interface can briefly exist before an address is assigned. Preserve it
    # in the diagnostic output, but only an addressed interface proves usable
    # IP connectivity.
    for path in Path("/sys/class/net").glob("zt*"):
        grouped.setdefault(path.name, [])
    return [{
        "id": network_id or interface,
        "name": "uložená ZeroTier síť" if network_id else "ZeroTier síť",
        "status": "OK" if addresses else "čeká na adresu",
        "type": None,
        "interface": interface,
        "addresses": addresses,
    } for interface, addresses in sorted(grouped.items())]


def status() -> dict:
    remembered = remembered_network_id()
    cli = _executable("zerotier-cli")
    if not cli:
        return {
            "installed": False, "online": False, "networks": [],
            "remembered_network_id": remembered, "detail": "ZeroTier není nainstalovaný.",
        }
    info = _run([cli, "-j", "info"])
    networks = _run([cli, "-j", "listnetworks"])
    if info.returncode or networks.returncode:
        detail = (info.stderr or networks.stderr or info.stdout or networks.stdout).strip()
        access_denied = "authtoken.secret" in detail or "permission denied" in detail.lower()
        visible = _visible_networks(remembered) if access_denied else []
        inferred_online = any(network["addresses"] for network in visible)
        return {
            "installed": True, "online": inferred_online, "networks": visible,
            "status_available": not access_denied,
            "remembered_network_id": remembered,
            "cli_path": cli,
            "detail": (
                "Stav byl určen podle lokálního ZeroTier rozhraní; podrobnosti daemonu "
                "nejsou bez administrátorského oprávnění dostupné."
                if inferred_online else
                "ZeroTier je nainstalovaný, ale MultiCam nevidí rozhraní s přidělenou IP. "
                "Ověřte autorizaci sítě příkazem sudo zerotier-cli listnetworks."
                if access_denied else detail or
                "ZeroTier je nainstalovaný, ale CLI není dostupné pro tohoto uživatele. Zkontrolujte službu zerotier-one a oprávnění."
            ),
        }
    try:
        node = json.loads(info.stdout)
        joined = json.loads(networks.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ZeroTierError("ZeroTier vrátil neplatný stav") from error
    return {
        "installed": True,
        "online": bool(node.get("online")),
        "status_available": True,
        "remembered_network_id": remembered,
        "node_id": node.get("address"),
        "version": node.get("version"),
        "cli_path": cli,
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
    pkexec = _executable("pkexec")
    if not pkexec:
        suffix = f" {normalized}" if normalized else ""
        raise ZeroTierError(f"Chybí pkexec. Použijte v terminálu: sudo ./scripts/setup-zerotier.sh {'--install' if install else ''}{suffix}")
    cli = _executable("zerotier-cli")
    if install or not cli:
        command = [pkexec, str(project_dir / "scripts" / "setup-zerotier.sh"), "--install"]
        if normalized:
            command.append(normalized)
    else:
        command = [pkexec, cli, "join", normalized]
    result = _run(command, timeout=180)
    if result.returncode:
        raise ZeroTierError((result.stderr or result.stdout).strip() or "Připojení k ZeroTier síti selhalo")
    if normalized:
        _remember_network_id(normalized)
    return {"accepted": True, "network_id": normalized or None, "detail": (result.stdout or "Požadavek byl odeslán.").strip()}
