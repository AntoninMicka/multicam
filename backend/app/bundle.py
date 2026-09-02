"""Portable, checksum-verified session bundles.

The bundle keeps original recordings byte-for-byte. Completed upload chunks and
playback proxies are deliberately omitted because they are reconstructible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID


BUNDLE_VERSION = "1.0"
MANIFEST_NAME = "multicam-bundle.json"


class BundleError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _included_files(session_dir: Path) -> list[Path]:
    result = []
    for path in session_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(session_dir)
        if "chunks" in relative.parts and ".uploads" in relative.parts:
            continue
        if path.name.endswith(".playback.webm") or path.suffix == ".part":
            continue
        result.append(path)
    return sorted(result)


def _write_bundle(
    session_dir: Path,
    session_id: UUID,
    destination: Path,
    files: list[Path],
    *,
    scope: str,
    take_id: UUID | None = None,
) -> Path:
    entries = []
    for path in files:
        relative = path.relative_to(session_dir).as_posix()
        entries.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "scope": scope,
        "session_id": str(session_id),
        "take_id": str(take_id) if take_id else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "layout": "session-directory",
        "files": entries,
        "processing": {
            "preferred_backends": ["comfyui", "ollama"],
            "topdown_output": "analysis/<take_id>/topdown-analysis.json",
        },
    }
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2), compress_type=zipfile.ZIP_DEFLATED)
            for path in files:
                relative = path.relative_to(session_dir).as_posix()
                compression = zipfile.ZIP_STORED if path.suffix.lower() in {".webm", ".mp4", ".mov"} else zipfile.ZIP_DEFLATED
                archive.write(path, f"session/{relative}", compress_type=compression)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def export_session(data_dir: Path, session_id: UUID, destination: Path) -> Path:
    session_dir = data_dir.resolve() / str(session_id)
    if not (session_dir / "session.json").is_file():
        raise BundleError(f"Session {session_id} was not found")
    return _write_bundle(session_dir, session_id, destination, _included_files(session_dir), scope="session")


def export_take(
    data_dir: Path,
    session_id: UUID,
    take_id: UUID,
    capture_ids: set[UUID],
    destination: Path,
) -> Path:
    session_dir = data_dir.resolve() / str(session_id)
    if not (session_dir / "session.json").is_file():
        raise BundleError(f"Session {session_id} was not found")
    selected = {str(capture_id) for capture_id in capture_ids}
    files: set[Path] = set()
    for name in ("session.json", "events.jsonl", "analysis.json", "report.json"):
        path = session_dir / name
        if path.is_file():
            files.add(path)
    take_analysis = session_dir / "analysis" / str(take_id)
    if take_analysis.is_dir():
        files.update(path for path in take_analysis.rglob("*") if path.is_file())
    for metadata_path in session_dir.glob("devices/*/.uploads/*/upload.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("capture_id") not in selected or not metadata.get("complete"):
            continue
        files.add(metadata_path)
        receipt_path = metadata.get("receipt", {}).get("file_path")
        if receipt_path:
            artifact = (data_dir.resolve() / receipt_path).resolve()
            if artifact.is_file() and artifact.is_relative_to(session_dir):
                files.add(artifact)
    if not any(path.suffix.lower() in {".webm", ".mp4", ".mov"} for path in files):
        raise BundleError(f"Take {take_id} has no completed recordings")
    return _write_bundle(session_dir, session_id, destination, sorted(files), scope="take", take_id=take_id)


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not name.startswith("session/"):
        raise BundleError(f"Unsafe archive member: {name}")
    return path


def import_session(data_dir: Path, source: Path) -> UUID:
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as archive:
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (KeyError, json.JSONDecodeError) as error:
            raise BundleError("Bundle manifest is missing or invalid") from error
        if manifest.get("bundle_version") != BUNDLE_VERSION:
            raise BundleError(f"Unsupported bundle version: {manifest.get('bundle_version')}")
        if manifest.get("scope", "session") != "session":
            raise BundleError("A take bundle is intended for processing and cannot be imported as a full session")
        try:
            session_id = UUID(manifest["session_id"])
        except (KeyError, ValueError) as error:
            raise BundleError("Bundle session_id is invalid") from error
        destination = data_dir / str(session_id)
        if destination.exists():
            raise BundleError(f"Session {session_id} already exists")
        expected = {item["path"]: item for item in manifest.get("files", [])}
        archive_names = {name.removeprefix("session/") for name in archive.namelist() if name.startswith("session/") and not name.endswith("/")}
        if archive_names != set(expected):
            raise BundleError("Bundle file list does not match its manifest")
        staging = Path(tempfile.mkdtemp(prefix=f".{session_id}-", dir=data_dir))
        try:
            for relative, metadata in expected.items():
                member = _safe_member(f"session/{relative}")
                target = staging.joinpath(*member.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(str(member)) as bundled, target.open("wb") as output:
                    for chunk in iter(lambda: bundled.read(1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                    raise BundleError(f"Checksum mismatch: {relative}")
            session = json.loads((staging / "session.json").read_text(encoding="utf-8"))
            if session.get("session_id") != str(session_id):
                raise BundleError("Session manifest does not match bundle session_id")
            os.replace(staging, destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return session_id


def import_take(data_dir: Path, source: Path, expected_session_id: UUID, expected_take_id: UUID) -> int:
    """Verify and merge a replicated take without replacing existing local data."""
    data_dir = data_dir.resolve()
    destination = data_dir / str(expected_session_id)
    if not (destination / "session.json").is_file():
        raise BundleError("The shared session does not exist on this backend yet")
    with zipfile.ZipFile(source, "r") as archive:
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (KeyError, json.JSONDecodeError) as error:
            raise BundleError("Bundle manifest is missing or invalid") from error
        if manifest.get("bundle_version") != BUNDLE_VERSION or manifest.get("scope") != "take":
            raise BundleError("Expected a take bundle")
        if manifest.get("session_id") != str(expected_session_id) or manifest.get("take_id") != str(expected_take_id):
            raise BundleError("Bundle identifiers do not match the request")
        expected = {item["path"]: item for item in manifest.get("files", [])}
        archive_names = {name.removeprefix("session/") for name in archive.namelist() if name.startswith("session/") and not name.endswith("/")}
        if archive_names != set(expected):
            raise BundleError("Bundle file list does not match its manifest")
        # Global files differ legitimately on each backend. Device artifacts are
        # immutable and can therefore be merged safely by checksum.
        ignored = {"session.json", "events.jsonl", "analysis.json", "report.json"}
        staging = Path(tempfile.mkdtemp(prefix=".federation-take-", dir=data_dir))
        imported = 0
        try:
            # Validate every member and every existing-file conflict before the
            # destination is changed.
            for relative, metadata in expected.items():
                member = _safe_member(f"session/{relative}")
                digest = hashlib.sha256()
                size = 0
                temporary = staging.joinpath(*PurePosixPath(relative).parts)
                temporary.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(str(member)) as bundled, temporary.open("wb") as output:
                    for chunk in iter(lambda: bundled.read(1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                    raise BundleError(f"Checksum mismatch: {relative}")
                if relative in ignored:
                    continue
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if target.exists():
                    if target.stat().st_size != size or _sha256(target) != digest.hexdigest():
                        raise BundleError(f"Conflicting replicated file: {relative}")
            for relative in expected:
                if relative in ignored:
                    continue
                temporary = staging.joinpath(*PurePosixPath(relative).parts)
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temporary, target)
                    imported += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or import a MultiCam session bundle")
    parser.add_argument("--data-dir", type=Path, default=Path("data/sessions"))
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("session_id", type=UUID)
    export.add_argument("destination", type=Path)
    import_ = commands.add_parser("import")
    import_.add_argument("source", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "export":
        print(export_session(arguments.data_dir, arguments.session_id, arguments.destination))
    else:
        print(import_session(arguments.data_dir, arguments.source))


if __name__ == "__main__":
    main()
