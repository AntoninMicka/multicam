import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from .models import UploadCreate, UploadReceipt, UploadStatus


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
        metadata = {"upload_id": str(upload_id), **data.model_dump(mode="json"), "complete": False}
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


uploads = UploadService()
