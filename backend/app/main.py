import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    Device,
    CaptureMedia,
    DeviceRegistration,
    DeviceState,
    Session,
    SessionCreate,
    SessionState,
    SocketMessage,
    UploadCreate,
    UploadReceipt,
    UploadStatus,
)
from .store import SessionNotFoundError, store
from .uploads import UploadConflictError, UploadNotFoundError, uploads
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


@app.get("/api/hotspot")
async def hotspot_status() -> dict:
    status_path = Path(os.environ.get("MULTICAM_HOTSPOT_STATUS", "/run/multicam/hotspot.json"))
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": False}


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


async def require_device(session_id: UUID, device_id: UUID) -> None:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    if str(device_id) not in session.devices:
        raise HTTPException(status_code=404, detail="Device not found")


@app.post(
    "/api/sessions/{session_id}/devices/{device_id}/uploads",
    response_model=UploadStatus,
    status_code=status.HTTP_201_CREATED,
)
async def create_upload(session_id: UUID, device_id: UUID, data: UploadCreate) -> UploadStatus:
    await require_device(session_id, device_id)
    result = await uploads.create(session_id, device_id, data)
    updated = await store.set_device_state(session_id, device_id, DeviceState.UPLOADING)
    await connections.broadcast(session_id, {"type": "session.updated", "payload": updated.model_dump(mode="json")})
    return result


@app.get(
    "/api/sessions/{session_id}/devices/{device_id}/uploads/{upload_id}",
    response_model=UploadStatus,
)
async def get_upload(session_id: UUID, device_id: UUID, upload_id: UUID) -> UploadStatus:
    await require_device(session_id, device_id)
    try:
        return uploads.status(session_id, device_id, upload_id)
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Upload not found") from error


@app.put(
    "/api/sessions/{session_id}/devices/{device_id}/uploads/{upload_id}/chunks/{index}",
    response_model=UploadStatus,
)
async def put_upload_chunk(
    session_id: UUID,
    device_id: UUID,
    upload_id: UUID,
    index: int,
    request: Request,
    chunk_sha256: str = Header(alias="X-Chunk-SHA256", pattern=r"^[0-9a-f]{64}$"),
) -> UploadStatus:
    await require_device(session_id, device_id)
    try:
        result = await uploads.put_chunk(session_id, device_id, upload_id, index, await request.body(), chunk_sha256)
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Upload not found") from error
    except UploadConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await connections.broadcast(session_id, {
        "type": "upload.progress",
        "payload": {
            "device_id": str(device_id),
            "received_chunks": len(result.received_chunks),
            "total_chunks": result.total_chunks,
        },
    })
    return result


@app.post(
    "/api/sessions/{session_id}/devices/{device_id}/uploads/{upload_id}/complete",
    response_model=UploadReceipt,
)
async def complete_upload(session_id: UUID, device_id: UUID, upload_id: UUID) -> UploadReceipt:
    await require_device(session_id, device_id)
    try:
        receipt = await uploads.complete(session_id, device_id, upload_id)
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Upload not found") from error
    except UploadConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    next_state = DeviceState.VERIFIED if uploads.capture_verified(session_id, device_id, receipt.capture_id) else DeviceState.UPLOADING
    updated = await store.set_device_state(session_id, device_id, next_state)
    await connections.broadcast(session_id, {"type": "session.updated", "payload": updated.model_dump(mode="json")})
    return receipt


@app.get("/api/sessions/{session_id}/media", response_model=list[CaptureMedia])
async def list_session_media(session_id: UUID) -> list[CaptureMedia]:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return uploads.list_media(session)


@app.get("/api/media/{session_id}/{device_id}/{capture_id}/video", response_class=FileResponse)
async def get_recording_media(session_id: UUID, device_id: UUID, capture_id: UUID) -> FileResponse:
    try:
        path = uploads.artifact_path(session_id, device_id, capture_id, "recording")
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Recording not found") from error
    return FileResponse(path)


@app.get("/api/media/{session_id}/{device_id}/{capture_id}/telemetry")
async def get_recording_telemetry(session_id: UUID, device_id: UUID, capture_id: UUID) -> list[dict]:
    try:
        path = uploads.artifact_path(session_id, device_id, capture_id, "telemetry")
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Telemetry not found") from error
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
            if message.type == "recording.start":
                session = await store.set_state(session_id, SessionState.RECORDING)
                await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
            elif message.type == "recording.stop":
                session = await store.set_state(session_id, SessionState.STOPPED)
                await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
            await connections.broadcast(session_id, message.model_dump(mode="json"))
            if message.type == "recording.start":
                asyncio.create_task(trigger_delayed_clap(session_id))
    except WebSocketDisconnect:
        connections.disconnect(session_id, websocket)
        if device_id is not None:
            await store.set_connected(session_id, device_id, False)
            await connections.broadcast(session_id, {"type": "session.updated", "payload": (await store.get(session_id)).model_dump(mode="json")})


async def trigger_delayed_clap(session_id: UUID) -> None:
    await asyncio.sleep(2)
    try:
        session = await store.get(session_id)
    except SessionNotFoundError:
        return
    if session.state != SessionState.RECORDING:
        return
    await connections.broadcast(session_id, {
        "type": "clap.trigger",
        "payload": {"requested_at": datetime.now(timezone.utc).isoformat(), "automatic": True},
    })


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
