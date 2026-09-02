import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .models import CaptureMedia, Session, UploadCreate, UploadReceipt, UploadStatus
from .clap import detect_flash


class UploadError(Exception):
    pass


class UploadNotFoundError(UploadError):
    pass


class UploadConflictError(UploadError):
    pass


class UploadService:
    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("MULTICAM_DATA_DIR")
        self.root = root or Path(configured or "data/sessions").resolve()
        self._locks: dict[UUID, asyncio.Lock] = {}

    def _device_dir(self, session_id: UUID, device_id: UUID) -> Path:
        return self.root / str(session_id) / "devices" / str(device_id)

    def _upload_dir(self, session_id: UUID, device_id: UUID, upload_id: UUID) -> Path:
        return self._device_dir(session_id, device_id) / ".uploads" / str(upload_id)

    def _metadata_path(self, session_id: UUID, device_id: UUID, upload_id: UUID) -> Path:
        return self._upload_dir(session_id, device_id, upload_id) / "upload.json"

    def _read_metadata(self, session_id: UUID, device_id: UUID, upload_id: UUID) -> dict:
        path = self._metadata_path(session_id, device_id, upload_id)
        if not path.is_file():
            raise UploadNotFoundError(upload_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_metadata(self, path: Path, metadata: dict) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    async def create(self, session_id: UUID, device_id: UUID, data: UploadCreate) -> UploadStatus:
        uploads_dir = self._device_dir(session_id, device_id) / ".uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        for metadata_path in uploads_dir.glob("*/upload.json"):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata["sha256"] == data.sha256
                and metadata["size_bytes"] == data.size_bytes
                and metadata.get("capture_id") == str(data.capture_id)
                and metadata.get("kind", "recording") == data.kind
            ):
                return self.status(session_id, device_id, UUID(metadata["upload_id"]))

        upload_id = uuid4()
        upload_dir = self._upload_dir(session_id, device_id, upload_id)
        (upload_dir / "chunks").mkdir(parents=True)
        metadata = {
            "upload_id": str(upload_id),
            **data.model_dump(mode="json"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "complete": False,
        }
        self._write_metadata(upload_dir / "upload.json", metadata)
        return self.status(session_id, device_id, upload_id)

    def status(self, session_id: UUID, device_id: UUID, upload_id: UUID) -> UploadStatus:
        metadata = self._read_metadata(session_id, device_id, upload_id)
        chunks_dir = self._upload_dir(session_id, device_id, upload_id) / "chunks"
        received = sorted(int(path.stem) for path in chunks_dir.glob("*.chunk"))
        return UploadStatus(
            upload_id=upload_id,
            received_chunks=received,
            total_chunks=metadata["total_chunks"],
            size_bytes=metadata["size_bytes"],
            complete=metadata.get("complete", False),
        )

    async def put_chunk(
        self,
        session_id: UUID,
        device_id: UUID,
        upload_id: UUID,
        index: int,
        content: bytes,
        expected_sha256: str,
    ) -> UploadStatus:
        metadata = self._read_metadata(session_id, device_id, upload_id)
        if metadata.get("complete"):
            return self.status(session_id, device_id, upload_id)
        if index < 0 or index >= metadata["total_chunks"]:
            raise UploadConflictError("Chunk index is outside upload range")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise UploadConflictError("Chunk checksum mismatch")
        expected_size = metadata["chunk_size"]
        if index == metadata["total_chunks"] - 1:
            expected_size = metadata["size_bytes"] - index * metadata["chunk_size"]
        if len(content) != expected_size:
            raise UploadConflictError("Chunk size mismatch")

        chunk_path = self._upload_dir(session_id, device_id, upload_id) / "chunks" / f"{index:08d}.chunk"
        if chunk_path.exists():
            if hashlib.sha256(chunk_path.read_bytes()).hexdigest() != actual_sha256:
                raise UploadConflictError("Chunk already exists with different content")
            return self.status(session_id, device_id, upload_id)
        temporary = chunk_path.with_suffix(".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, chunk_path)
        return self.status(session_id, device_id, upload_id)

    async def complete(self, session_id: UUID, device_id: UUID, upload_id: UUID) -> UploadReceipt:
        lock = self._locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            metadata = self._read_metadata(session_id, device_id, upload_id)
            if metadata.get("complete"):
                return UploadReceipt.model_validate(metadata["receipt"])
            status = self.status(session_id, device_id, upload_id)
            if len(status.received_chunks) != metadata["total_chunks"]:
                raise UploadConflictError("Upload has missing chunks")

            if metadata.get("kind", "recording") == "telemetry":
                output_dir = self._device_dir(session_id, device_id) / "telemetry"
                final_path = output_dir / f"{metadata['capture_id']}.timing.jsonl"
            else:
                extension = Path(metadata["file_name"]).suffix.lower()
                if extension not in {".webm", ".mp4", ".mov"}:
                    extension = ".bin"
                output_dir = self._device_dir(session_id, device_id) / "recordings"
                final_path = output_dir / f"{metadata['capture_id']}{extension}"
            output_dir.mkdir(parents=True, exist_ok=True)
            temporary = final_path.with_suffix(final_path.suffix + ".part")
            digest = hashlib.sha256()
            size = 0
            with temporary.open("wb") as destination:
                for index in range(metadata["total_chunks"]):
                    content = (self._upload_dir(session_id, device_id, upload_id) / "chunks" / f"{index:08d}.chunk").read_bytes()
                    destination.write(content)
                    digest.update(content)
                    size += len(content)
                destination.flush()
                os.fsync(destination.fileno())
            if size != metadata["size_bytes"] or digest.hexdigest() != metadata["sha256"]:
                temporary.unlink(missing_ok=True)
                raise UploadConflictError("Final file integrity check failed")
            os.replace(temporary, final_path)
            receipt = UploadReceipt(
                upload_id=upload_id,
                capture_id=UUID(metadata["capture_id"]),
                kind=metadata.get("kind", "recording"),
                receipt_id=uuid4(),
                file_path=str(final_path.relative_to(self.root)),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
            metadata["complete"] = True
            metadata["receipt"] = receipt.model_dump(mode="json")
            self._write_metadata(self._metadata_path(session_id, device_id, upload_id), metadata)
            return receipt

    def capture_verified(self, session_id: UUID, device_id: UUID, capture_id: UUID) -> bool:
        uploads_dir = self._device_dir(session_id, device_id) / ".uploads"
        completed: set[str] = set()
        for metadata_path in uploads_dir.glob("*/upload.json"):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("capture_id") == str(capture_id) and metadata.get("complete"):
                completed.add(metadata.get("kind", "recording"))
        return completed == {"recording", "telemetry"}

    def _take_id(self, recording: dict, telemetry: dict | None) -> UUID | None:
        if recording.get("take_id"):
            return UUID(recording["take_id"])
        if not telemetry or not telemetry.get("receipt"):
            return None
        path = self.root / telemetry["receipt"]["file_path"]
        if not path.is_file() or not path.resolve().is_relative_to(self.root):
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                sample = json.loads(line)
                if sample.get("event") != "sync_marker":
                    continue
                requested_at = sample.get("details", {}).get("requested_at")
                if requested_at:
                    return uuid5(NAMESPACE_URL, f"multicam-clap:{requested_at}")
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return None

    def list_media(self, session: Session) -> list[CaptureMedia]:
        result: list[CaptureMedia] = []
        try:
            sync_analysis = json.loads((self.root / str(session.session_id) / "analysis.json").read_text(encoding="utf-8")).get("captures", {})
        except (OSError, ValueError):
            sync_analysis = {}
        for device in session.devices.values():
            uploads_dir = self._device_dir(session.session_id, device.device_id) / ".uploads"
            captures: dict[str, dict[str, dict]] = {}
            for metadata_path in uploads_dir.glob("*/upload.json"):
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("complete") and metadata.get("receipt"):
                    capture_id = metadata.get("capture_id", metadata["upload_id"])
                    captures.setdefault(capture_id, {})[metadata.get("kind", "recording")] = metadata
            for capture_id, artifacts in captures.items():
                if "recording" not in artifacts:
                    continue
                recording = artifacts["recording"]
                receipt_path = self.root / recording["receipt"]["file_path"]
                created_at = recording.get("created_at")
                if created_at is None and receipt_path.exists():
                    created_at = datetime.fromtimestamp(receipt_path.stat().st_mtime, timezone.utc)
                telemetry_url = None
                if "telemetry" in artifacts:
                    telemetry_url = f"/api/media/{session.session_id}/{device.device_id}/{capture_id}/telemetry"
                result.append(CaptureMedia(
                    capture_id=UUID(capture_id),
                    take_id=self._take_id(recording, artifacts.get("telemetry")),
                    device_id=device.device_id,
                    device_name=device.name,
                    role=device.role,
                    mime_type=recording["mime_type"],
                    size_bytes=recording["size_bytes"],
                    created_at=created_at,
                    video_url=f"/api/media/{session.session_id}/{device.device_id}/{capture_id}/video",
                    telemetry_url=telemetry_url,
                    sync_point_seconds=(
                        sync_analysis.get(capture_id, {}).get("flash_seconds")
                        if sync_analysis.get(capture_id, {}).get("status") == "detected" else None
                    ),
                ))
        return sorted(result, key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc))

    def artifact_path(self, session_id: UUID, device_id: UUID, capture_id: UUID, kind: str) -> Path:
        uploads_dir = self._device_dir(session_id, device_id) / ".uploads"
        for metadata_path in uploads_dir.glob("*/upload.json"):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata.get("capture_id", metadata.get("upload_id")) == str(capture_id)
                and metadata.get("kind", "recording") == kind
                and metadata.get("complete")
            ):
                path = self.root / metadata["receipt"]["file_path"]
                if path.is_file() and path.resolve().is_relative_to(self.root):
                    return path
        raise UploadNotFoundError(capture_id)

    def playback_path(self, session_id: UUID, device_id: UUID, capture_id: UUID) -> Path:
        source = self.artifact_path(session_id, device_id, capture_id, "recording")
        if source.suffix.lower() != ".webm":
            return source
        playback = source.with_name(f"{source.stem}.playback.webm")
        if playback.is_file() and playback.stat().st_size > 0 and playback.stat().st_mtime >= source.stat().st_mtime:
            return playback
        temporary = playback.with_suffix(".webm.part")
        temporary.unlink(missing_ok=True)
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "webm", str(temporary),
        ], capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            return source
        os.replace(temporary, playback)
        return playback

    def delete_capture(self, session_id: UUID, device_id: UUID, capture_id: UUID) -> bool:
        uploads_dir = self._device_dir(session_id, device_id) / ".uploads"
        matched: list[tuple[Path, dict]] = []
        for metadata_path in uploads_dir.glob("*/upload.json"):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("capture_id", metadata.get("upload_id")) == str(capture_id):
                matched.append((metadata_path.parent, metadata))
        if not matched:
            return False
        root = self.root.resolve()
        for upload_dir, metadata in matched:
            receipt_path = metadata.get("receipt", {}).get("file_path")
            if receipt_path:
                artifact = (self.root / receipt_path).resolve()
                if artifact.is_relative_to(root):
                    artifact.unlink(missing_ok=True)
                    if artifact.suffix.lower() == ".webm":
                        artifact.with_name(f"{artifact.stem}.playback.webm").unlink(missing_ok=True)
            resolved_upload = upload_dir.resolve()
            if resolved_upload.is_relative_to(root) and resolved_upload.parent == uploads_dir.resolve():
                shutil.rmtree(resolved_upload)
        return True

    def delete_take(self, session: Session, take_id: UUID) -> int:
        captures = [media for media in self.list_media(session) if (media.take_id or media.capture_id) == take_id]
        return sum(self.delete_capture(session.session_id, media.device_id, media.capture_id) for media in captures)

    def build_report(self, session: Session) -> dict:
        analysis_path = self.root / str(session.session_id) / "analysis.json"
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            analysis = {"captures": {}}
        event_log_path = self.root / str(session.session_id) / "events.jsonl"
        try:
            events = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            events = []
        local_backend_id = os.environ.get("MULTICAM_BACKEND_ID_RUNTIME")
        expected_devices = {
            str(device.device_id): {"name": device.name, "role": device.role.value}
            for device in session.devices.values()
            if not local_backend_id or not device.owner_backend_id or device.owner_backend_id == local_backend_id
        }
        takes: dict[str, dict] = {}
        for media in self.list_media(session):
            take_id = str(media.take_id or media.capture_id)
            take = takes.setdefault(take_id, {
                "take_id": take_id,
                "created_at": media.created_at.isoformat() if media.created_at else None,
                "streams": [],
            })
            artifacts: dict[str, dict] = {}
            uploads_dir = self._device_dir(session.session_id, media.device_id) / ".uploads"
            for metadata_path in uploads_dir.glob("*/upload.json"):
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("capture_id") == str(media.capture_id) and metadata.get("complete"):
                    receipt = metadata.get("receipt", {})
                    artifacts[metadata.get("kind", "recording")] = {
                        "file_path": receipt.get("file_path"),
                        "size_bytes": receipt.get("size_bytes", metadata.get("size_bytes")),
                        "sha256": receipt.get("sha256", metadata.get("sha256")),
                    }
            take["streams"].append({
                "capture_id": str(media.capture_id),
                "device_id": str(media.device_id),
                "device_name": media.device_name,
                "role": media.role.value,
                "mime_type": media.mime_type,
                "artifacts": artifacts,
                "sync_analysis": analysis.get("captures", {}).get(str(media.capture_id)),
            })
        for take in takes.values():
            received = {stream["device_id"] for stream in take["streams"]}
            take["received_device_ids"] = sorted(received)
            take["missing_device_ids"] = sorted(set(expected_devices) - received)
            take["complete"] = not take["missing_device_ids"] and all(
                {"recording", "telemetry"}.issubset(stream["artifacts"]) for stream in take["streams"]
            )
        report = {
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "session": {
                "session_id": str(session.session_id),
                "name": session.name,
                "created_at": session.created_at.isoformat(),
                "state": session.state.value,
            },
            "expected_devices": expected_devices,
            "takes": sorted(takes.values(), key=lambda item: item["created_at"] or ""),
            "events": events,
        }
        report_path = self.root / str(session.session_id) / "report.json"
        self._write_metadata(report_path, report)
        return report

    def append_session_event(self, session_id: UUID, event: dict) -> None:
        session_dir = self.root / str(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / "events.jsonl").open("a", encoding="utf-8") as destination:
            destination.write(json.dumps(event, ensure_ascii=False) + "\n")

    def analyze_claps(self, session: Session) -> dict:
        captures: dict[str, dict] = {}
        for media in self.list_media(session):
            if not media.telemetry_url:
                captures[str(media.capture_id)] = {"status": "not_found", "detail": "Chybí telemetrie."}
                continue
            telemetry_path = self.artifact_path(session.session_id, media.device_id, media.capture_id, "telemetry")
            markers: list[dict] = []
            for line in telemetry_path.read_text(encoding="utf-8").splitlines():
                sample = json.loads(line)
                if sample.get("event") == "sync_marker" and sample.get("recording_offset_ms") is not None:
                    markers.append({
                        "expected_seconds": float(sample["recording_offset_ms"]) / 1000,
                        **sample.get("details", {}),
                    })
            if not markers:
                captures[str(media.capture_id)] = {"status": "not_found", "detail": "Telemetrie neobsahuje sync_marker."}
                continue
            video_path = self.artifact_path(session.session_id, media.device_id, media.capture_id, "recording")
            sequence_steps = [
                {**marker, **detect_flash(video_path, marker["expected_seconds"])}
                for marker in markers
            ]
            primary = next((step for step in sequence_steps if step.get("phase") == "sync"), sequence_steps[0])
            captures[str(media.capture_id)] = {**primary, "sequence_steps": sequence_steps}
        detected = [item["flash_seconds"] for item in captures.values() if item.get("status") == "detected"]
        reference = min(detected) if detected else None
        if reference is not None:
            for item in captures.values():
                if item.get("flash_seconds") is not None:
                    item["timeline_correction_seconds"] = round(reference - item["flash_seconds"], 4)
        analysis = {"schema_version": "1.0", "reference_flash_seconds": reference, "captures": captures}
        self._write_metadata(self.root / str(session.session_id) / "analysis.json", analysis)
        return analysis


uploads = UploadService()
