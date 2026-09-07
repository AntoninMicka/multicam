import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.storage_guard import check_storage, StorageUnavailableError


def configure(monkeypatch, tmp_path, *, mounted=True, identifier='ssd-123', mode='rw'):
    mount = tmp_path / 'ssd'
    mount.mkdir()
    (mount / '.multicam-storage-id').write_text(identifier)
    monkeypatch.setenv('MULTICAM_REQUIRED_STORAGE_MOUNT', str(mount))
    monkeypatch.setenv('MULTICAM_STORAGE_ID', 'ssd-123')
    monkeypatch.setenv('MULTICAM_DATA_DIR', str(mount / 'data' / 'sessions'))
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == '/proc/self/mountinfo':
            return f'36 25 8:1 / {mount} {mode} - ext4 /dev/sda1 {mode}\n' if mounted else ''
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    return mount


def test_valid_ssd_and_no_probe_files_left(monkeypatch, tmp_path):
    mount = configure(monkeypatch, tmp_path)
    check_storage()
    assert [item.name for item in mount.iterdir()] == ['.multicam-storage-id']


@pytest.mark.parametrize('options', [{'mounted': False}, {'identifier': 'wrong'}, {'mode': 'ro'}])
def test_missing_wrong_or_readonly_disk_is_rejected(monkeypatch, tmp_path, options):
    configure(monkeypatch, tmp_path, **options)
    with pytest.raises(StorageUnavailableError):
        check_storage()


def test_data_directory_outside_ssd_is_rejected(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('MULTICAM_DATA_DIR', str(tmp_path / 'flash'))
    with pytest.raises(StorageUnavailableError):
        check_storage()
    assert not (tmp_path / 'flash').exists()


def test_missing_ssd_prevents_initialization_writes(tmp_path):
    env = {**os.environ, 'MULTICAM_REQUIRED_STORAGE_MOUNT': str(tmp_path / 'unmounted'),
           'MULTICAM_STORAGE_ID': 'ssd-123', 'MULTICAM_DATA_DIR': str(tmp_path / 'unmounted' / 'data'),
           'MULTICAM_BACKEND_ID_FILE': str(tmp_path / 'backend-id'), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-c', 'import backend.app.main'], env=env,
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'StorageUnavailableError' in result.stderr
    assert not (tmp_path / 'backend-id').exists()
    assert not (tmp_path / 'unmounted').exists()
