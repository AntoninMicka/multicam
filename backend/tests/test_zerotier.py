from pathlib import Path

import pytest

from app.zerotier import ZeroTierError, join


def test_network_id_is_optional_only_for_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.zerotier.shutil.which", lambda command: None)
    with pytest.raises(ZeroTierError, match="Network ID povinné"):
        join("", tmp_path, install=False)
    with pytest.raises(ZeroTierError, match="Chybí pkexec") as error:
        join("", tmp_path, install=True)
    assert "--install" in str(error.value)


def test_invalid_network_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ZeroTierError, match="16 hexadecimálních"):
        join("not-a-network", tmp_path, install=True)
