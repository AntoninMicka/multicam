import asyncio
from uuid import UUID, uuid4

from .models import Device, DeviceRegistration, DeviceState, Session, SessionCreate, SessionState, utc_now


class SessionNotFoundError(KeyError):
    pass


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[UUID, Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, data: SessionCreate) -> Session:
        session = Session(name=data.name)
        async with self._lock:
            self._sessions[session.session_id] = session
        return session.model_copy(deep=True)

    async def list(self) -> list[Session]:
        async with self._lock:
            return [session.model_copy(deep=True) for session in self._sessions.values()]

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
            return session.model_copy(deep=True)

    async def current(self) -> Session:
        async with self._lock:
            if not self._sessions:
                raise SessionNotFoundError("current")
            session = next(reversed(self._sessions.values()))
            return session.model_copy(deep=True)

    async def set_state(self, session_id: UUID, state: SessionState) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            session.state = state
            device_state = DeviceState.RECORDING if state == SessionState.RECORDING else DeviceState.STORED
            for device in session.devices.values():
                if device.connected:
                    device.state = device_state
            return session.model_copy(deep=True)


store = SessionStore()
