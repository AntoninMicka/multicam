"""Optional SSD requirement for router deployments; checked before any data writes."""
import os
import tempfile
from pathlib import Path


class StorageUnavailableError(OSError):
    pass


def check_storage() -> None:
    configured = os.getenv("MULTICAM_REQUIRED_STORAGE_MOUNT")
    if not configured:
        return
    mount = Path(configured).resolve()
    expected = os.getenv("MULTICAM_STORAGE_ID", "")
    if not expected:
        raise StorageUnavailableError("MULTICAM_STORAGE_ID není nastavené")
    # ismount() misses bind mounts on the same device as the LXC root filesystem.
    entries = Path("/proc/self/mountinfo").read_text().splitlines()
    found = False
    for entry in entries:
        fields = entry.split()
        if len(fields) < 7:
            continue
        target = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
        if target == str(mount) and "rw" in fields[5].split(','):
            found = True
            break
    if not found:
        raise StorageUnavailableError(f"SSD není připojené pro zápis: {mount}")
    try:
        if (mount / ".multicam-storage-id").read_text().strip() != expected:
            raise StorageUnavailableError("Připojeno jiné úložiště, než je nakonfigurované SSD")
        root = Path(os.getenv("MULTICAM_DATA_DIR", "data/sessions")).resolve()
        if not root.is_relative_to(mount):
            raise StorageUnavailableError("Datový adresář neleží na povinném SSD")
        # Actual write+fsync detects read-only devices and disconnected I/O.
        with tempfile.TemporaryFile(dir=mount) as probe:
            probe.write(b"multicam\n")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as error:
        raise StorageUnavailableError(f"SSD není dostupné: {error}") from error


if __name__ == "__main__":
    check_storage()
