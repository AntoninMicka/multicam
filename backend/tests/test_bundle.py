import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from app.bundle import BundleError, export_session, export_take, import_session


def test_bundle_round_trip_and_excludes_reconstructible_files(tmp_path: Path) -> None:
    session_id = uuid4()
    source_root = tmp_path / "source"
    session = source_root / str(session_id)
    recording = session / "devices" / str(uuid4()) / "recordings" / "capture.webm"
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b"original-video")
    (session / "session.json").write_text(json.dumps({"session_id": str(session_id)}))
    proxy = recording.with_name("capture.playback.webm")
    proxy.write_bytes(b"proxy")
    chunks = session / "devices" / "device" / ".uploads" / "upload" / "chunks" / "000.chunk"
    chunks.parent.mkdir(parents=True)
    chunks.write_bytes(b"duplicate")

    bundle = export_session(source_root, session_id, tmp_path / "take.multicam.zip")
    with zipfile.ZipFile(bundle) as archive:
        assert "session/devices/device/.uploads/upload/chunks/000.chunk" not in archive.namelist()
        assert not any(name.endswith(".playback.webm") for name in archive.namelist())

    imported_root = tmp_path / "imported"
    assert import_session(imported_root, bundle) == session_id
    assert (imported_root / str(session_id) / recording.relative_to(session)).read_bytes() == b"original-video"


def test_bundle_rejects_checksum_mismatch(tmp_path: Path) -> None:
    session_id = uuid4()
    root = tmp_path / "source"
    session = root / str(session_id)
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"session_id": str(session_id)}))
    bundle = export_session(root, session_id, tmp_path / "session.multicam.zip")
    corrupted = tmp_path / "corrupted.multicam.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(corrupted, "w") as target:
        for item in source.infolist():
            content = source.read(item)
            target.writestr(item, b"changed" if item.filename == "session/session.json" else content)
    with pytest.raises(BundleError, match="Checksum mismatch"):
        import_session(tmp_path / "target", corrupted)


def test_take_bundle_contains_only_selected_capture(tmp_path: Path) -> None:
    session_id = uuid4()
    take_id = uuid4()
    selected_capture = uuid4()
    other_capture = uuid4()
    root = tmp_path / "sessions"
    session = root / str(session_id)
    (session / "session.json").parent.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps({"session_id": str(session_id)}))
    for capture_id, content in ((selected_capture, b"selected"), (other_capture, b"other")):
        device = session / "devices" / str(uuid4())
        recording = device / "recordings" / f"{capture_id}.webm"
        recording.parent.mkdir(parents=True)
        recording.write_bytes(content)
        metadata = device / ".uploads" / str(uuid4()) / "upload.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({
            "capture_id": str(capture_id), "complete": True,
            "receipt": {"file_path": str(recording.relative_to(root))},
        }))

    bundle = export_take(root, session_id, take_id, {selected_capture}, tmp_path / "take.multicam.zip")
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("multicam-bundle.json"))
        names = archive.namelist()
    assert manifest["scope"] == "take"
    assert manifest["take_id"] == str(take_id)
    assert any(str(selected_capture) in name for name in names)
    assert not any(str(other_capture) in name for name in names)
    with pytest.raises(BundleError, match="cannot be imported"):
        import_session(tmp_path / "target", bundle)
