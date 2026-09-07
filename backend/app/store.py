import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .storage_guard import check_storage

from .models import Device, DeviceRegistration, DeviceRole, DeviceState, Session, SessionCreate, SessionState, utc_now


class SessionNotFoundError(KeyError):
    pass


class SessionStore:
    def __init__(self, root: Path | None = None) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._lock = asyncio.Lock()
        configured = os.environ.get("MULTICAM_DATA_DIR")
        self.root = root or Path(configured or "data/sessions").resolve()
        self.active_session_id: UUID | None = None
        self.active_changed_at: datetime | None = None
        self.active_backend_id: str | None = None
        self._load()
        self._load_active()
        for session in self._sessions.values():
            if session.session_id != self.active_session_id and session.state != SessionState.CLOSED:
                session.state = SessionState.CLOSED
                session.closed_at = utc_now()
                self._persist(session)

    def _load_active(self) -> None:
        try:
            data = json.loads((self.root / ".active-session.json").read_text(encoding="utf-8"))
            session_id = UUID(data["session_id"]) if data.get("session_id") else None
            if session_id is None or (session_id in self._sessions and self._sessions[session_id].state != SessionState.CLOSED):
                self.active_session_id = session_id
                self.active_changed_at = datetime.fromisoformat(data["changed_at"])
                self.active_backend_id = data.get("backend_id")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _persist_active(self) -> None:
        check_storage()
        if not self.active_changed_at:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".active-session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "session_id": str(self.active_session_id) if self.active_session_id else None,
            "changed_at": self.active_changed_at.isoformat(),
            "backend_id": self.active_backend_id,
        }), encoding="utf-8")
        os.replace(temporary, path)

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
        check_storage()
        session_dir = self.root / str(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / "session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(session.model_dump(mode="json"), indent=2), encoding="utf-8")
        os.replace(temporary, path)

    async def create(self, data: SessionCreate) -> Session:
        session = Session(name=data.name)
        async with self._lock:
            if self.active_session_id is not None:
                raise ValueError("Nejprve ukončete aktuální relaci")
            self._sessions[session.session_id] = session
            self._persist(session)
            self.active_session_id = session.session_id
            self.active_changed_at = utc_now()
            self.active_backend_id = os.environ.get("MULTICAM_BACKEND_ID_RUNTIME")
            self._persist_active()
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

    async def register_device(
        self, session_id: UUID, data: DeviceRegistration,
        owner_backend_id: str | None = None, owner_backend_name: str | None = None,
    ) -> Device:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            if session.state == SessionState.CLOSED:
                raise ValueError("Ukončená relace nepřijímá další kamery")
            if data.role in {DeviceRole.MAIN_CAMERA, DeviceRole.TOP_CAMERA}:
                occupied = next((d for d in session.devices.values() if d.role == data.role and d.device_id != data.device_id), None)
                if occupied:
                    raise ValueError(f"Tuto roli už používá {occupied.name}")
            existing = session.devices.get(str(data.device_id))
            if existing and existing.owner_backend_id not in {None, owner_backend_id}:
                raise ValueError("Zařízení patří jinému backendu")
            device = Device(
                device_id=data.device_id or uuid4(),
                name=data.name,
                role=data.role,
                capabilities=data.capabilities,
                owner_backend_id=owner_backend_id,
                owner_backend_name=owner_backend_name,
            )
            session.devices[str(device.device_id)] = device
            self._persist(session)
            return device.model_copy(deep=True)

    async def merge_remote(
        self, remote: Session, remote_backend_id: str, local_backend_id: str,
        *, authoritative: bool = False,
    ) -> Session:
        """Merge only the devices owned by a remote backend into a shared session."""
        async with self._lock:
            session = self._sessions.get(remote.session_id)
            if session is None:
                if not authoritative:
                    raise SessionNotFoundError(remote.session_id)
                session = remote.model_copy(deep=True)
                session.devices = {}
                self._sessions[session.session_id] = session
            for key, device in remote.devices.items():
                owner = device.owner_backend_id or remote_backend_id
                if owner == remote_backend_id or (authoritative and owner != local_backend_id):
                    existing = session.devices.get(key)
                    if existing and existing.last_seen_at > device.last_seen_at:
                        continue
                    device.owner_backend_id = owner
                    session.devices[key] = device.model_copy(deep=True)
            # Control revisions prevent an older poll from undoing a live command.
            if authoritative and remote.state_revision >= session.state_revision:
                session.name = remote.name
                if session.state != SessionState.CLOSED:
                    session.state = remote.state
                    session.closed_at = remote.closed_at
                    session.state_revision = remote.state_revision
                    session.last_control = remote.last_control
                if session.state == SessionState.CLOSED and self.active_session_id == session.session_id:
                    self.active_session_id = None
                    self.active_changed_at = remote.closed_at or utc_now()
                    self._persist_active()
            self._persist(session)
            return session.model_copy(deep=True)

    async def set_device_source(self, session_id: UUID, device_id: UUID, source_kind: str) -> Device:
        async with self._lock:
            device = self._sessions[session_id].devices[str(device_id)]
            device.source_kind = source_kind
            self._persist(self._sessions[session_id])
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
            session = self._sessions.get(self.active_session_id) if self.active_session_id else None
            if session is None or session.state == SessionState.CLOSED:
                raise SessionNotFoundError("current")
            return session.model_copy(deep=True)

    async def activate(
        self, session_id: UUID, backend_id: str | None = None,
        changed_at: datetime | None = None, *, force: bool = False,
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            if session.state == SessionState.CLOSED:
                raise ValueError("Ukončenou relaci nelze znovu aktivovat")
            if self.active_session_id and self.active_session_id != session_id:
                raise ValueError("Nejprve ukončete aktuální relaci")
            candidate = changed_at or utc_now()
            if not force and self.active_changed_at and changed_at and candidate <= self.active_changed_at:
                current = self._sessions.get(self.active_session_id)
                return (current or session).model_copy(deep=True)
            self.active_session_id = session_id
            self.active_changed_at = candidate
            self.active_backend_id = backend_id
            self._persist_active()
            return session.model_copy(deep=True)

    def active_state(self) -> dict | None:
        if not self.active_changed_at:
            return None
        return {
            "session_id": str(self.active_session_id) if self.active_session_id else None,
            "changed_at": self.active_changed_at.isoformat(),
            "backend_id": self.active_backend_id,
        }

    async def clear_active(self, changed_at: datetime, backend_id: str) -> None:
        async with self._lock:
            # The director may have closed and locally deleted a session while
            # this peer was offline. Its explicit empty current state is final.
            if self.active_session_id:
                session = self._sessions.get(self.active_session_id)
                if session:
                    session.state = SessionState.CLOSED
                    session.state_revision += 1
                    session.last_control = None
                    session.closed_at = session.closed_at or changed_at
                    self._persist(session)
            self.active_session_id = None
            self.active_changed_at = changed_at
            self.active_backend_id = backend_id
            self._persist_active()

    async def set_state(self, session_id: UUID, state: SessionState, *, revision: int | None = None, control: dict | None = None) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            if session.state == SessionState.CLOSED and state != SessionState.CLOSED:
                raise ValueError("Ukončenou relaci nelze znovu otevřít")
            if state == SessionState.CLOSED:
                if session.state == SessionState.RECORDING:
                    raise ValueError("Nejprve zastavte nahrávání")
                session.closed_at = session.closed_at or utc_now()
                if self.active_session_id == session_id:
                    self.active_session_id = None
                    self.active_changed_at = session.closed_at
                    self._persist_active()
            session.state = state
            session.state_revision = revision if revision is not None else session.state_revision + 1
            session.last_control = control
            self._persist(session)
            return session.model_copy(deep=True)

    async def delete(self, session_id: UUID) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            if session.state != SessionState.CLOSED:
                raise ValueError("Smazat lze pouze ukončenou relaci")
            session_dir = self.root / str(session_id)
            self._sessions.pop(session_id)
            if self.active_session_id == session_id:
                self.active_session_id = None
                self.active_changed_at = None
                self.active_backend_id = None
                (self.root / ".active-session.json").unlink(missing_ok=True)
            if session_dir.is_dir():
                shutil.rmtree(session_dir)


store = SessionStore()
