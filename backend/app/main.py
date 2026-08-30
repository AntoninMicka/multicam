from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .models import Device, DeviceRegistration, Session, SessionCreate, SocketMessage
from .store import SessionNotFoundError, store
from .websocket import connections

app = FastAPI(title="MultiCam control server", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
async def create_session(data: SessionCreate) -> Session:
    return await store.create(data)


@app.get("/api/sessions", response_model=list[Session])
async def list_sessions() -> list[Session]:
    return await store.list()


@app.get("/api/sessions/current", response_model=Session)
async def get_current_session() -> Session:
    try:
        return await store.current()
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="No active session") from error


@app.get("/api/sessions/{session_id}", response_model=Session)
async def get_session(session_id: UUID) -> Session:
    try:
        return await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error


@app.post("/api/sessions/{session_id}/devices", response_model=Device, status_code=status.HTTP_201_CREATED)
async def register_device(session_id: UUID, data: DeviceRegistration) -> Device:
    try:
        device = await store.register_device(session_id, data)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    await connections.broadcast(session_id, {"type": "session.updated", "payload": (await store.get(session_id)).model_dump(mode="json")})
    return device


@app.websocket("/api/ws/{session_id}")
async def session_socket(websocket: WebSocket, session_id: UUID, device_id: UUID | None = None) -> None:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError:
        await websocket.close(code=4404, reason="Session not found")
        return
    await connections.connect(session_id, websocket)
    await websocket.send_json({"type": "session.snapshot", "payload": session.model_dump(mode="json")})
    try:
        while True:
            message = SocketMessage.model_validate(await websocket.receive_json())
            await connections.broadcast(session_id, message.model_dump(mode="json"))
    except WebSocketDisconnect:
        connections.disconnect(session_id, websocket)
        if device_id is not None:
            await store.set_connected(session_id, device_id, False)
            await connections.broadcast(session_id, {"type": "session.updated", "payload": (await store.get(session_id)).model_dump(mode="json")})


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
