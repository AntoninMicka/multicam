"""Linux network-interface addresses exposed as frontend URLs."""

from __future__ import annotations

import fcntl
import os
import socket
import struct
from pathlib import Path

SIOCGIFADDR = 0x8915


def _up_interfaces() -> list[str]:
    result = []
    for path in Path("/sys/class/net").glob("*"):
        try:
            if (path / "operstate").read_text(encoding="ascii").strip() in {"up", "unknown"}:
                result.append(path.name)
        except OSError:
            continue
    return result


def interface_addresses() -> list[dict[str, str]]:
    """Return non-loopback addresses for active interfaces on Debian/Ubuntu."""
    interfaces = _up_interfaces()
    addresses: list[tuple[str, str, str]] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for name in interfaces:
                try:
                    raw = fcntl.ioctl(sock, SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
                    address = socket.inet_ntoa(raw[20:24])
                    if not address.startswith("127."):
                        addresses.append((name, "ipv4", address))
                except OSError:
                    pass
    except OSError:
        pass
    try:
        lines = Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) != 6 or fields[5] not in interfaces:
            continue
        address = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(fields[0]))
        if address != "::1":
            addresses.append((fields[5], "ipv6", address))

    scheme = "https" if os.getenv("MULTICAM_HTTPS", "1") != "0" else "http"
    port = os.getenv("MULTICAM_PORT", "8000")
    result = []
    for name, family, address in sorted(set(addresses)):
        if family == "ipv6":
            scope = f"%25{name}" if address.lower().startswith("fe80:") else ""
            host = f"[{address}{scope}]"
        else:
            host = address
        result.append({"interface": name, "family": family, "address": address, "url": f"{scheme}://{host}:{port}/"})
    return result
