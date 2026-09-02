import asyncio
import hmac
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .bundle import BundleError, export_session, export_take, import_take
from .discovery import discovery
from .federation import federation
from .models import (
    Device,
    CaptureMedia,
    DeviceRegistration,
    DeviceRole,
    DeviceState,
    Session,
    SessionCreate,
    SessionState,
    SocketMessage,
    UploadCreate,
    UploadReceipt,
    UploadStatus,
)
from .mosaic import MosaicError, render_mosaic
from .store import SessionNotFoundError, store
from .uploads import UploadConflictError, UploadNotFoundError, uploads
from .websocket import connections
from .vision import VisionRequest, run_vision_job

@asynccontextmanager
async def lifespan(_: FastAPI):
    os.environ["MULTICAM_BACKEND_ID_RUNTIME"] = discovery.backend_id
    await discovery.start()
    sync_task = asyncio.create_task(federation_sync_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
        await discovery.stop()


app = FastAPI(title="MultiCam control server", version="0.1.0", lifespan=lifespan)
active_clap_sequences: set[UUID] = set()
analysis_tasks: set[asyncio.Task] = set()
federation_tasks: set[asyncio.Task] = set()
peer_active_sessions: dict[str, dict | None] = {}
deleted_sessions_path = uploads.root / ".federation-deleted.json"
try:
    deleted_session_ids: set[UUID] = {UUID(value) for value in json.loads(deleted_sessions_path.read_text(encoding="utf-8"))}
except (OSError, ValueError, TypeError):
    deleted_session_ids = set()


def persist_deleted_sessions() -> None:
    path = uploads.root / ".federation-deleted.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(str(value) for value in deleted_session_ids)), encoding="utf-8")
    os.replace(temporary, path)


def clap_sequence_steps(session: Session) -> list[tuple[str, Device | None]]:
    connected = [device for device in session.devices.values() if device.connected]
    main = next((device for device in connected if device.role == DeviceRole.MAIN_CAMERA), None)
    # The top camera observes the sequence and must never emit its own flash.
    secondary = [device for device in connected if device.role == DeviceRole.SECONDARY_CAMERA]
    steps: list[tuple[str, Device | None]] = []
    if main:
        steps.append(("sync", main))
    steps.extend(("camera_id", device) for device in secondary)
    if main:
        steps.extend([("main_signature", main), ("main_signature", main)])
    return steps
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


@app.get("/api/backends")
async def list_backends() -> dict:
    peers = discovery.snapshot()
    for peer in peers:
        peer["active_session"] = peer_active_sessions.get(peer["backend_id"])
    return {
        "self": {"backend_id": discovery.backend_id, "name": discovery.name, "url": discovery.advertised_url()},
        "peers": peers,
        "discovery_enabled": discovery.enabled,
        "federation_enabled": federation.enabled,
        "transfer_enabled": federation.transfer_enabled,
    }


def require_federation_token(value: str | None) -> None:
    if not federation.enabled or not value or not hmac.compare_digest(value, federation.token):
        raise HTTPException(status_code=401, detail="Invalid federation token")


@app.get("/api/federation/snapshot")
async def federation_snapshot(x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    sessions = [item for item in await store.list() if item.session_id not in deleted_session_ids]
    return {
        "backend_id": discovery.backend_id,
        "sessions": [item.model_dump(mode="json") for item in sessions],
        "deleted_session_ids": [str(value) for value in sorted(deleted_session_ids, key=str)],
    }


@app.post("/api/federation/delete-session")
async def federation_delete_session(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    session_id = UUID((await request.json())["session_id"])
    await delete_session_data(session_id, relay=False)
    return {"deleted": True}


@app.post("/api/federation/control")
async def federation_control(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    data = await request.json()
    await apply_control(UUID(data["session_id"]), SocketMessage.model_validate(data["message"]), relay=False)
    return {"accepted": True}


@app.post("/api/federation/event")
async def federation_event(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    data = await request.json()
    await connections.broadcast(UUID(data["session_id"]), SocketMessage.model_validate(data["message"]).model_dump(mode="json"))
    return {"accepted": True}


@app.post("/api/federation/take")
async def federation_take(
    request: Request,
    session_id: UUID,
    take_id: UUID,
    source_backend_id: str,
    x_multicam_federation: str | None = Header(default=None),
) -> dict:
    require_federation_token(x_multicam_federation)
    if not federation.transfer_enabled:
        raise HTTPException(status_code=403, detail="Federation data transfer is disabled")
    descriptor, temporary_name = tempfile.mkstemp(prefix="multicam-federation-", suffix=".zip")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as output:
            async for chunk in request.stream():
                output.write(chunk)
        imported = await asyncio.to_thread(import_take, uploads.root, temporary, session_id, take_id)
    except BundleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        temporary.unlink(missing_ok=True)
    return {"imported_files": imported, "source_backend_id": source_backend_id}


async def federation_sync_loop() -> None:
    while True:
        if federation.enabled:
            for peer in discovery.snapshot():
                try:
                    snapshot = await federation.get_snapshot(peer["url"])
                    for deleted_id in snapshot.get("deleted_session_ids", []):
                        remote_deleted = UUID(deleted_id)
                        if remote_deleted not in deleted_session_ids:
                            try:
                                await delete_session_data(remote_deleted, relay=False)
                            except HTTPException:
                                pass
                    remote_sessions = [Session.model_validate(raw) for raw in snapshot["sessions"]]
                    active = next((item for item in remote_sessions if item.state != SessionState.STOPPED), None)
                    peer_active_sessions[snapshot["backend_id"]] = active.model_dump(mode="json") if active else None
                    for remote in remote_sessions:
                        if remote.session_id in deleted_session_ids:
                            continue
                        merged = await store.merge_remote(remote, snapshot["backend_id"], discovery.backend_id)
                        await connections.broadcast(merged.session_id, {"type": "session.updated", "payload": merged.model_dump(mode="json")})
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        await asyncio.sleep(2)


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


async def delete_session_data(session_id: UUID, *, relay: bool) -> None:
    try:
        await store.delete(session_id)
    except SessionNotFoundError:
        # A federated delete is idempotent.
        if relay:
            raise HTTPException(status_code=404, detail="Session not found")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    deleted_session_ids.add(session_id)
    persist_deleted_sessions()
    await connections.broadcast(session_id, {"type": "session.deleted", "payload": {"session_id": str(session_id)}})
    if relay:
        task = asyncio.create_task(federation.broadcast_json("/api/federation/delete-session", {"session_id": str(session_id)}))
        federation_tasks.add(task)
        task.add_done_callback(federation_tasks.discard)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: UUID) -> dict:
    await delete_session_data(session_id, relay=True)
    return {"deleted": True, "session_id": str(session_id)}


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
        device = await store.register_device(session_id, data, discovery.backend_id)
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
    if next_state == DeviceState.VERIFIED and federation.enabled and federation.transfer_enabled:
        media = next((item for item in uploads.list_media(updated) if item.capture_id == receipt.capture_id), None)
        if media:
            task = asyncio.create_task(replicate_completed_take(session_id, media.take_id or media.capture_id))
            federation_tasks.add(task)
            task.add_done_callback(federation_tasks.discard)
    return receipt


async def replicate_completed_take(session_id: UUID, take_id: UUID) -> None:
    session = await store.get(session_id)
    report = uploads.build_report(session)
    take = next((item for item in report["takes"] if item["take_id"] == str(take_id)), None)
    if not take or not take["complete"]:
        return
    local_ids = {
        media.capture_id for media in uploads.list_media(session)
        if (media.take_id or media.capture_id) == take_id
        and (not session.devices[str(media.device_id)].owner_backend_id
             or session.devices[str(media.device_id)].owner_backend_id == discovery.backend_id)
    }
    if not local_ids:
        return
    destination = uploads.root / ".federation" / f"{session_id}-{take_id}-{discovery.backend_id}.zip"
    await asyncio.to_thread(export_take, uploads.root, session_id, take_id, local_ids, destination)
    await asyncio.gather(*(
        federation.send_bundle(peer["url"], destination, str(session_id), str(take_id))
        for peer in discovery.snapshot()
    ), return_exceptions=True)


@app.get("/api/sessions/{session_id}/media", response_model=list[CaptureMedia])
async def list_session_media(session_id: UUID) -> list[CaptureMedia]:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return uploads.list_media(session)


@app.get("/api/sessions/{session_id}/report")
async def get_session_report(session_id: UUID) -> dict:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return uploads.build_report(session)


@app.get("/api/sessions/{session_id}/bundle", response_class=FileResponse)
async def get_session_bundle(session_id: UUID) -> FileResponse:
    try:
        await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    destination = uploads.root / ".exports" / f"{session_id}.multicam.zip"
    try:
        await asyncio.to_thread(export_session, uploads.root, session_id, destination)
    except BundleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        destination,
        media_type="application/zip",
        filename=f"{session_id}.multicam.zip",
    )


@app.get("/api/sessions/{session_id}/takes/{take_id}/bundle", response_class=FileResponse)
async def get_take_bundle(session_id: UUID, take_id: UUID) -> FileResponse:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    captures = [media for media in uploads.list_media(session) if (media.take_id or media.capture_id) == take_id]
    if not captures:
        raise HTTPException(status_code=404, detail="Recording group not found")
    destination = uploads.root / ".exports" / f"{session_id}-{take_id}.multicam.zip"
    try:
        await asyncio.to_thread(
            export_take, uploads.root, session_id, take_id,
            {media.capture_id for media in captures}, destination,
        )
    except BundleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        destination,
        media_type="application/zip",
        filename=f"take-{take_id}.multicam.zip",
    )


@app.get("/api/sessions/{session_id}/takes/{take_id}/mosaic", response_class=FileResponse)
async def get_take_mosaic(session_id: UUID, take_id: UUID) -> FileResponse:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    captures = [media for media in uploads.list_media(session) if (media.take_id or media.capture_id) == take_id]
    if not captures:
        raise HTTPException(status_code=404, detail="Recording group not found")
    # Stable role order makes the matrix predictable: main, top, then sides.
    role_order = {DeviceRole.MAIN_CAMERA: 0, DeviceRole.TOP_CAMERA: 1, DeviceRole.SECONDARY_CAMERA: 2}
    captures.sort(key=lambda media: (role_order[media.role], media.device_name, str(media.capture_id)))
    try:
        sources = [uploads.playback_path(session_id, media.device_id, media.capture_id) for media in captures]
    except UploadNotFoundError as error:
        raise HTTPException(status_code=409, detail="A recording is missing") from error
    main_index = next((index for index, media in enumerate(captures) if media.role == DeviceRole.MAIN_CAMERA), None)
    destination = uploads.root / ".exports" / f"{session_id}-{take_id}-mosaic.mp4"
    try:
        await asyncio.to_thread(
            render_mosaic, sources, [media.sync_point_seconds for media in captures], destination, main_index,
        )
    except MosaicError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return FileResponse(destination, media_type="video/mp4", filename=f"take-{take_id}-mosaic.mp4")


@app.post("/api/sessions/{session_id}/takes/{take_id}/topdown-analysis", status_code=status.HTTP_202_ACCEPTED)
async def start_topdown_analysis(session_id: UUID, take_id: UUID, request: VisionRequest) -> dict:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    captures = [media for media in uploads.list_media(session) if (media.take_id or media.capture_id) == take_id]
    top = next((media for media in captures if media.role == DeviceRole.TOP_CAMERA), None)
    if top is None:
        raise HTTPException(status_code=409, detail="This take has no top-over recording")
    try:
        video = uploads.artifact_path(session_id, top.device_id, top.capture_id, "recording")
    except UploadNotFoundError as error:
        raise HTTPException(status_code=409, detail="Top-over recording is missing") from error
    job_id = uuid4()
    job_dir = uploads.root / str(session_id) / "analysis" / str(take_id) / "vision" / str(job_id)
    metadata = {
        "schema_version": "1.0", "job_id": str(job_id), "session_id": str(session_id),
        "take_id": str(take_id), "capture_id": str(top.capture_id), "device_id": str(top.device_id),
    }
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(json.dumps({
        **metadata, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
        "request": request.model_dump(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    task = asyncio.create_task(asyncio.to_thread(run_vision_job, video, job_dir, request, metadata))
    analysis_tasks.add(task)
    task.add_done_callback(analysis_tasks.discard)
    return {**metadata, "status": "queued", "status_url": f"/api/analysis-jobs/{job_id}"}


@app.get("/api/analysis-jobs/{job_id}")
async def get_analysis_job(job_id: UUID) -> dict:
    matches = list(uploads.root.glob(f"*/analysis/*/vision/{job_id}/job.json"))
    if not matches:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail="Analysis job state is invalid") from error


@app.post("/api/sessions/{session_id}/analyze-claps")
async def analyze_session_claps(session_id: UUID) -> dict:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    return await asyncio.to_thread(uploads.analyze_claps, session)


@app.get("/api/media/{session_id}/{device_id}/{capture_id}/video", response_class=FileResponse)
async def get_recording_media(session_id: UUID, device_id: UUID, capture_id: UUID) -> FileResponse:
    try:
        path = await asyncio.to_thread(uploads.playback_path, session_id, device_id, capture_id)
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Recording not found") from error
    return FileResponse(path)


@app.delete("/api/media/{session_id}/{device_id}/{capture_id}")
async def delete_recording(session_id: UUID, device_id: UUID, capture_id: UUID) -> dict:
    await require_device(session_id, device_id)
    if not uploads.delete_capture(session_id, device_id, capture_id):
        raise HTTPException(status_code=404, detail="Recording not found")
    return {"deleted": True, "capture_id": str(capture_id)}


@app.delete("/api/sessions/{session_id}/takes/{take_id}")
async def delete_take(session_id: UUID, take_id: UUID) -> dict:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    deleted = uploads.delete_take(session, take_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recording group not found")
    return {"deleted": deleted, "take_id": str(take_id)}


@app.get("/api/media/{session_id}/{device_id}/{capture_id}/telemetry")
async def get_recording_telemetry(session_id: UUID, device_id: UUID, capture_id: UUID) -> list[dict]:
    try:
        path = uploads.artifact_path(session_id, device_id, capture_id, "telemetry")
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Telemetry not found") from error
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def apply_control(session_id: UUID, message: SocketMessage, *, relay: bool) -> None:
    current = await store.get(session_id)
    local_devices = [
        device for device in current.devices.values()
        if not device.owner_backend_id or device.owner_backend_id == discovery.backend_id
    ]
    if message.type == "control.arm":
        session = await store.set_state(session_id, SessionState.ARMED)
    elif message.type == "recording.start":
        unready = [device.name for device in local_devices if device.connected and device.state != DeviceState.ARMED]
        if unready:
            raise HTTPException(status_code=409, detail=f"Kamery bez ARM: {', '.join(unready)}")
        message.payload.setdefault("take_id", str(uuid4()))
        session = await store.set_state(session_id, SessionState.RECORDING)
    elif message.type == "recording.stop":
        session = await store.set_state(session_id, SessionState.STOPPED)
    else:
        raise HTTPException(status_code=400, detail="Unsupported federation control")
    await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
    await connections.broadcast(session_id, message.model_dump(mode="json"))
    if relay:
        task = asyncio.create_task(federation.broadcast_json("/api/federation/control", {
            "session_id": str(session_id), "message": message.model_dump(mode="json"),
        }))
        federation_tasks.add(task)
        task.add_done_callback(federation_tasks.discard)
    if message.type == "recording.start":
        asyncio.create_task(trigger_delayed_clap(session_id))


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
            if message.type == "clock.ping":
                server_received_ms = datetime.now(timezone.utc).timestamp() * 1000
                await websocket.send_json({
                    "type": "clock.pong",
                    "payload": {
                        **message.payload,
                        "server_received_ms": server_received_ms,
                        "server_sent_ms": datetime.now(timezone.utc).timestamp() * 1000,
                    },
                })
                continue
            if message.type == "clap.sequence.request":
                asyncio.create_task(run_clap_sequence(session_id, automatic=False))
                continue
            if message.type in {"control.arm", "recording.start", "recording.stop"}:
                try:
                    await apply_control(session_id, message, relay=True)
                except HTTPException as control_error:
                    await websocket.send_json({
                        "type": "control.rejected",
                        "payload": {"command_id": message.payload.get("command_id"), "detail": control_error.detail},
                    })
                continue
            if message.type == "clock.report" and device_id is not None:
                message.payload["device_id"] = str(device_id)
            elif message.type == "upload.client_status" and device_id is not None:
                message.payload["device_id"] = str(device_id)
            elif message.type == "preview.frame" and device_id is not None:
                message.payload["device_id"] = str(device_id)
            elif message.type == "control.ack" and device_id is not None:
                ack_state = {
                    "ready": DeviceState.ARMED,
                    "started": DeviceState.RECORDING,
                    "stopped": DeviceState.STORED,
                    "error": DeviceState.READY,
                }.get(message.payload.get("status"))
                if ack_state is not None:
                    session = await store.set_device_state(session_id, device_id, ack_state)
                    message.payload["device_id"] = str(device_id)
                    await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
            await connections.broadcast(session_id, message.model_dump(mode="json"))
            if message.type == "control.ack":
                task = asyncio.create_task(federation.broadcast_json("/api/federation/event", {
                    "session_id": str(session_id), "message": message.model_dump(mode="json"),
                }))
                federation_tasks.add(task)
                task.add_done_callback(federation_tasks.discard)
    except WebSocketDisconnect:
        connections.disconnect(session_id, websocket)
        if device_id is not None:
            await store.set_connected(session_id, device_id, False)
            await connections.broadcast(session_id, {"type": "session.updated", "payload": (await store.get(session_id)).model_dump(mode="json")})


async def trigger_delayed_clap(session_id: UUID) -> None:
    await asyncio.sleep(2)
    await run_clap_sequence(session_id, automatic=True)


async def run_clap_sequence(session_id: UUID, automatic: bool) -> None:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError:
        return
    if session.state != SessionState.RECORDING:
        return
    if session_id in active_clap_sequences:
        return
    active_clap_sequences.add(session_id)
    sequence_id = str(uuid4())
    steps = clap_sequence_steps(session)
    uploads.append_session_event(session_id, {
        "type": "clap.sequence.started", "sequence_id": sequence_id,
        "automatic": automatic, "created_at": datetime.now(timezone.utc).isoformat(),
        "step_count": len(steps),
    })
    for index, (phase, target) in enumerate(steps):
        current = await store.get(session_id)
        if current.state != SessionState.RECORDING:
            break
        payload = {
            "sequence_id": sequence_id, "step_index": index, "step_count": len(steps),
            "phase": phase, "target_device_id": str(target.device_id) if target else None,
            "target_device_name": target.name if target else None,
            "target_role": target.role.value if target else None,
            "requested_at": datetime.now(timezone.utc).isoformat(), "automatic": automatic,
        }
        uploads.append_session_event(session_id, {"type": "clap.step", **payload})
        await connections.broadcast(session_id, {"type": "clap.trigger", "payload": payload})
        await asyncio.sleep(1.1)
    uploads.append_session_event(session_id, {
        "type": "clap.sequence.completed", "sequence_id": sequence_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    active_clap_sequences.discard(session_id)


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
