from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceState(StrEnum):
    DISCONNECTED = "disconnected"
    READY = "ready"
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
