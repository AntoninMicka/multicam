import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .models import Device, DeviceRegistration, DeviceRole, DeviceState, Session, SessionCreate, SessionState, utc_now


class SessionNotFoundError(KeyError):
    pass


class SessionStore:
    def __init__(self, root: Path | None = None) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._lock = asyncio.Lock()
        configured = os.environ.get("MULTICAM_DATA_DIR")
        self.root = root or Path(configured or "data/sessions").resolve()
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for manifest_path in self.root.glob("*/session.json"):
            try:
                session = Session.model_validate_json(manifest_path.read_text(encoding="utf-8"))
                for device in session.devices.values():
                    device.connected = False
                self._sessions[session.session_id] = session
            except (OSError, ValueError):
                continue
        self._recover_legacy_sessions()

    def _recover_legacy_sessions(self) -> None:
        if not self.root.is_dir():
            return
        for session_dir in self.root.iterdir():
            if not session_dir.is_dir() or (session_dir / "session.json").exists():
                continue
            try:
                session_id = UUID(session_dir.name)
            except ValueError:
                continue
            device_dirs = list((session_dir / "devices").glob("*"))
            evidence = [path for path in session_dir.rglob("upload.json")]
            if not device_dirs or not evidence:
                continue
            timestamps = [path.stat().st_mtime for path in evidence]
            created_at = datetime.fromtimestamp(min(timestamps), timezone.utc)
            devices: dict[str, Device] = {}
            for device_dir in device_dirs:
                try:
                    device_id = UUID(device_dir.name)
                except ValueError:
                    continue
                devices[str(device_id)] = Device(
                    device_id=device_id,
                    name=f"Kamera {str(device_id)[:8]}",
                    role=DeviceRole.SECONDARY_CAMERA,
                    state=DeviceState.VERIFIED,
                    connected=False,
                    last_seen_at=created_at,
                )
            if not devices:
                continue
            session = Session(
                session_id=session_id,
                name=f"Obnovená relace {created_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
                state=SessionState.STOPPED,
                created_at=created_at,
                devices=devices,
            )
            self._sessions[session_id] = session
            self._persist(session)

    def _persist(self, session: Session) -> None:
        session_dir = self.root / str(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(session.model_dump(mode="json"), indent=2), encoding="utf-8")
        os.replace(temporary, path)

    async def create(self, data: SessionCreate) -> Session:
        session = Session(name=data.name)
        async with self._lock:
            self._sessions[session.session_id] = session
            self._persist(session)
        return session.model_copy(deep=True)

    async def list(self) -> list[Session]:
        async with self._lock:
            self._recover_legacy_sessions()
            sessions = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
            return [session.model_copy(deep=True) for session in sessions]

    async def get(self, session_id: UUID) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            return session.model_copy(deep=True)

    async def register_device(self, session_id: UUID, data: DeviceRegistration) -> Device:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            device = Device(
                device_id=data.device_id or uuid4(),
                name=data.name,
                role=data.role,
                capabilities=data.capabilities,
            )
            session.devices[str(device.device_id)] = device
            self._persist(session)
            return device.model_copy(deep=True)

    async def set_connected(self, session_id: UUID, device_id: UUID, connected: bool) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            device = session.devices.get(str(device_id))
            if device is not None:
                device.connected = connected
                device.last_seen_at = utc_now()
                self._persist(session)

    async def set_device_state(self, session_id: UUID, device_id: UUID, state: DeviceState) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            device = session.devices.get(str(device_id))
            if device is None:
                raise KeyError(device_id)
            device.state = state
            device.last_seen_at = utc_now()
            self._persist(session)
            return session.model_copy(deep=True)

    async def current(self) -> Session:
        async with self._lock:
            if not self._sessions:
                raise SessionNotFoundError("current")
            session = max(self._sessions.values(), key=lambda item: item.created_at)
            return session.model_copy(deep=True)

    async def set_state(self, session_id: UUID, state: SessionState) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            session.state = state
            self._persist(session)
            return session.model_copy(deep=True)


store = SessionStore()
