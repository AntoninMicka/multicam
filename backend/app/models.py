from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceState(StrEnum):
    DISCONNECTED = "disconnected"
    READY = "ready"
    ARMED = "armed"
    RECORDING = "recording"
    STORED = "stored"
    UPLOADING = "uploading"
    VERIFIED = "verified"


class DeviceRole(StrEnum):
    MAIN_CAMERA = "main_camera"
    TOP_CAMERA = "top_camera"
    SECONDARY_CAMERA = "secondary_camera"


class SessionState(StrEnum):
    CREATED = "created"
    ARMED = "armed"
    RECORDING = "recording"
    STOPPED = "stopped"


class DeviceCapabilities(BaseModel):
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    free_storage_bytes: int | None = Field(default=None, ge=0)
    camera_permission: bool | None = None
    microphone_permission: bool | None = None


class Device(BaseModel):
    device_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=80)
    role: DeviceRole
    state: DeviceState = DeviceState.READY
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)
    connected: bool = True
    last_seen_at: datetime = Field(default_factory=utc_now)
    owner_backend_id: str | None = None
    owner_backend_name: str | None = None


class DeviceRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: DeviceRole
    device_id: UUID | None = None
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class SessionCreate(BaseModel):
    name: str = Field(default="Nová relace", min_length=1, max_length=120)


class Session(BaseModel):
    schema_version: str = "1.0"
    session_id: UUID = Field(default_factory=uuid4)
    name: str
    state: SessionState = SessionState.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    devices: dict[str, Device] = Field(default_factory=dict)


class SocketMessage(BaseModel):
    type: str
    payload: dict = Field(default_factory=dict)


class UploadCreate(BaseModel):
    capture_id: UUID = Field(default_factory=uuid4)
    take_id: UUID | None = None
    kind: str = Field(default="recording", pattern=r"^(recording|telemetry)$")
    file_name: str = Field(min_length=1, max_length=160)
    mime_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_size: int = Field(ge=256 * 1024, le=16 * 1024 * 1024)
    total_chunks: int = Field(gt=0)


class UploadStatus(BaseModel):
    upload_id: UUID
    received_chunks: list[int]
    total_chunks: int
    size_bytes: int
    complete: bool = False


class UploadReceipt(BaseModel):
    upload_id: UUID
    capture_id: UUID
    kind: str
    receipt_id: UUID
    file_path: str
    size_bytes: int
    sha256: str
    verified: bool = True


class CaptureMedia(BaseModel):
    capture_id: UUID
    take_id: UUID | None = None
    device_id: UUID
    device_name: str
    role: DeviceRole
    mime_type: str
    size_bytes: int
    created_at: datetime | None = None
    video_url: str | None
    telemetry_url: str | None = None
    sync_point_seconds: float | None = None
    owner_backend_id: str | None = None
    owner_backend_name: str | None = None
    available_locally: bool = True
