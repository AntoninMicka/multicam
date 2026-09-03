import json
from pathlib import Path

import pytest

from app.zerotier import ZeroTierError, join, status


def test_network_id_is_optional_only_for_install(tmp_path: Path, monkeypatch) -> None:
    # _executable also searches the usual sbin directories explicitly, so the
    # test must isolate that adapter rather than only PATH lookup.
    monkeypatch.setattr("app.zerotier._executable", lambda command: None)
    with pytest.raises(ZeroTierError, match="Network ID povinné"):
        join("", tmp_path, install=False)
    with pytest.raises(ZeroTierError, match="Chybí pkexec") as error:
        join("", tmp_path, install=True)
    assert "--install" in str(error.value)


def test_invalid_network_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ZeroTierError, match="16 hexadecimálních"):
        join("not-a-network", tmp_path, install=True)


def test_cli_is_found_in_debian_sbin_when_path_does_not_contain_it(tmp_path: Path, monkeypatch) -> None:
    from app import zerotier
    cli = tmp_path / "zerotier-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(zerotier.shutil, "which", lambda name: None)
    monkeypatch.setattr(zerotier, "EXECUTABLE_DIRS", (str(tmp_path),))
    assert zerotier._executable("zerotier-cli") == str(cli)


def test_status_distinguishes_missing_cli_permission_from_offline(monkeypatch) -> None:
    from app import zerotier
    monkeypatch.setattr(zerotier, "_executable", lambda name: "/usr/sbin/zerotier-cli")
    monkeypatch.setattr(zerotier, "_run", lambda command: __import__("subprocess").CompletedProcess(
        command, 1, "", "authtoken.secret not found or readable",
    ))
    monkeypatch.setattr(zerotier, "_visible_networks", lambda network_id: [])
    result = status()
    assert result["installed"] is True
    assert result["status_available"] is False
    assert "nevidí rozhraní" in result["detail"]


def test_status_infers_online_from_addressed_zerotier_interface(monkeypatch) -> None:
    from app import zerotier
    monkeypatch.setattr(zerotier, "_executable", lambda name: "/usr/sbin/zerotier-cli")
    monkeypatch.setattr(zerotier, "_run", lambda command: __import__("subprocess").CompletedProcess(
        command, 1, "", "authtoken.secret not readable",
    ))
    monkeypatch.setattr(zerotier, "_visible_networks", lambda network_id: [{
        "id": network_id or "ztabc", "name": "ZeroTier síť", "status": "OK",
        "interface": "ztabc", "addresses": ["10.10.0.2"],
    }])
    result = status()
    assert result["online"] is True
    assert result["networks"][0]["addresses"] == ["10.10.0.2"]


def test_successful_join_remembers_network_id(tmp_path: Path, monkeypatch) -> None:
    from app import zerotier
    config = tmp_path / "zerotier.json"
    monkeypatch.setenv("MULTICAM_ZEROTIER_CONFIG", str(config))
    monkeypatch.delenv("MULTICAM_ZEROTIER_NETWORK", raising=False)
    monkeypatch.setattr(zerotier, "_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(zerotier, "_run", lambda command, timeout=15: __import__("subprocess").CompletedProcess(
        command, 0, "OK", "",
    ))
    network_id = "8056c2e21c000001"
    assert join(network_id, tmp_path)["accepted"] is True
    assert zerotier.remembered_network_id() == network_id
    assert json.loads(config.read_text())["network_id"] == network_id
