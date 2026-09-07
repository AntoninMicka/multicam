import asyncio
import hmac
import hashlib
import json
import os
import tempfile
import time
import subprocess
from urllib.parse import parse_qs, urlencode, urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .storage_guard import check_storage, StorageUnavailableError

# Fail before SessionStore or Federation can persist anything on router flash.
check_storage()

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
from .network import interface_addresses
from .store import SessionNotFoundError, store
from .uploads import UploadConflictError, UploadNotFoundError, uploads
from .ip_cameras import IPCameraService
from .media_validation import MediaValidationError

ip_cameras = IPCameraService(store, uploads)
from .websocket import connections
from .vision import VisionRequest, run_vision_job
from .zerotier import ZeroTierError, join as join_zerotier, status as zerotier_status

@asynccontextmanager
async def lifespan(_: FastAPI):
    os.environ["MULTICAM_BACKEND_ID_RUNTIME"] = discovery.backend_id
    await discovery.start()
    sync_task = asyncio.create_task(federation_sync_loop())
    transfer_task = asyncio.create_task(federation_transfer_loop())
    conversion_task = asyncio.create_task(uploads.normalize_existing_recordings())
    try:
        yield
    finally:
        sync_task.cancel()
        conversion_task.cancel()
        transfer_task.cancel()
        await asyncio.gather(sync_task, conversion_task, transfer_task, return_exceptions=True)
        await ip_cameras.stop()
        if ip_cameras.finishing:
            await asyncio.gather(*ip_cameras.finishing, return_exceptions=True)
        await discovery.stop()


app = FastAPI(title="MultiCam control server", version="0.1.0", lifespan=lifespan)
active_clap_sequences: set[UUID] = set()
analysis_tasks: set[asyncio.Task] = set()
federation_tasks: set[asyncio.Task] = set()
upload_leases: dict[UUID, tuple[UUID, float]] = {}
peer_active_sessions: dict[str, dict | None] = {}
deleted_sessions_path = uploads.root / ".local-deleted-sessions.json"
try:
    deleted_session_ids: set[UUID] = {UUID(value) for value in json.loads(deleted_sessions_path.read_text(encoding="utf-8"))}
except (OSError, ValueError, TypeError):
    deleted_session_ids = set()


def persist_deleted_sessions() -> None:
    path = uploads.root / ".local-deleted-sessions.json"
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


@app.middleware("http")
async def require_storage(request: Request, call_next):
    try:
        check_storage()
    except StorageUnavailableError as error:
        return JSONResponse(status_code=503, content={"detail": str(error)})
    return await call_next(request)


@app.get("/api/health")
async def health() -> dict[str, str]:
    try:
        check_storage()
    except StorageUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"status": "ok"}


@app.get("/api/backends")
async def list_backends() -> dict:
    peers = federation.target_peers() if federation.enabled else discovery.snapshot()
    for peer in peers:
        peer["active_session"] = peer_active_sessions.get(peer["backend_id"])
    return {
        "self": {"backend_id": discovery.backend_id, "name": discovery.name, "url": discovery.advertised_url()},
        "peers": peers,
        "discovery_enabled": discovery.enabled,
        "federation_enabled": federation.enabled,
        "transfer_enabled": federation.transfer_enabled,
        "federation_role": "peer",
        "discovery_diagnostics": discovery.diagnostics(),
    }


@app.post("/api/backends/ping")
async def ping_backends(request: Request) -> dict:
    require_local_operator(request)
    return {"results": await discovery.application_ping()}


def require_federation_token(value: str | None) -> None:
    if not federation.enabled or not value or not hmac.compare_digest(value, federation.token):
        raise HTTPException(status_code=401, detail="Invalid federation token")


def require_local_operator(request: Request) -> None:
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Pairing can only be configured from this notebook")


@app.get("/api/federation/config")
async def federation_config() -> dict:
    return {
        "enabled": federation.enabled,
        "transfer_enabled": federation.transfer_enabled,
        "token_fingerprint": hashlib.sha256(federation.token.encode()).hexdigest()[:12] if federation.token else None,
        "tls_verify": federation.tls_verify,
        "last_sync_at": federation.last_sync_at,
        "last_error": federation.last_error,
        "role": "peer",
        "is_director": federation.is_director,
        "is_storage": federation.is_storage,
        "peers": federation.target_peers(),
        **federation.assignments(),
    }


@app.get("/api/federation/transfers")
async def federation_transfers() -> dict:
    pending: list[dict] = []
    for session in await store.list():
        for take_id, fingerprint in completed_local_takes(session).items():
            for peer in federation.direct_transfer_peers():
                receipt = transfer_receipt(session.session_id, take_id, peer["backend_id"])
                if not transfer_is_current(receipt, fingerprint):
                    pending.append({"session_id": str(session.session_id), "take_id": str(take_id),
                                    "peer_backend_id": peer["backend_id"]})
    return {"pending_count": len(pending), "pending": pending,
            "deferred": not federation.transfer_enabled, "direction": "to_storage",
            "storage_backend_id": federation.storage_backend_id}


@app.patch("/api/federation/config")
async def update_federation_config(request: Request) -> dict:
    require_local_operator(request)
    data = await request.json()
    try:
        federation.configure(
            token=data.get("token"),
            transfer_enabled=data.get("transfer_enabled"),
        )
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return await federation_config()


assignment_lock = asyncio.Lock()
control_lock = asyncio.Lock()
storage_mutation_lock = asyncio.Lock()
applied_controls: dict[UUID, int] = {}


async def assign_backend_roles(data: dict) -> None:
    async with assignment_lock:
        if not federation.is_director:
            raise HTTPException(status_code=409, detail="Role přiděluje aktuální director")
        if any(item.state == SessionState.RECORDING for item in await store.list()):
            raise HTTPException(status_code=409, detail="Role lze předat až po zastavení nahrávání")
        director = data.get("director_backend_id", federation.director_backend_id)
        storage = data.get("storage_backend_id", federation.storage_backend_id)
        members = federation.membership()
        if director not in members or storage not in members:
            raise HTTPException(status_code=400, detail="Vyberte spárovaný backend")
        # Seed a new director with the latest session before handing over control.
        # The old director commits first, so a lost response cannot leave two directors.
        if director != discovery.backend_id:
            peer = next(p for p in federation.target_peers() if p["backend_id"] == director)
            await federation.post_json(peer["url"], "/api/federation/prepare-director",
                                       await federation_snapshot(federation.token))
        assignment = {"director_backend_id": director, "storage_backend_id": storage,
                      "assignment_revision": federation.assignment_revision + 1}
        federation.adopt_assignments(assignment)
        await federation.broadcast_json("/api/federation/assignments", assignment)
        await connections.broadcast_all({"type": "federation.config", "payload": {"is_director": federation.is_director}})


@app.post("/api/federation/roles")
async def change_backend_roles(request: Request) -> dict:
    require_local_operator(request)
    data = await request.json()
    try:
        if federation.is_director:
            async with control_lock:
                await assign_backend_roles(data)
        else:
            await federation.send_to_director("/api/federation/roles-request", data)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail=f"Role nelze předat: {error}") from error
    return await federation_config()


@app.post("/api/federation/roles-request")
async def roles_request(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    async with control_lock:
        await assign_backend_roles(await request.json())
    return {"accepted": True}


@app.post("/api/federation/prepare-director")
async def prepare_director(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    snapshot = await request.json()
    if snapshot.get("backend_id") != federation.director_backend_id:
        raise HTTPException(status_code=409, detail="Předání musí připravit aktuální director")
    await merge_federation_snapshot(snapshot, {"backend_id": federation.director_backend_id})
    return {"ready": True}


@app.post("/api/federation/assignments")
async def receive_assignments(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    federation.adopt_assignments(await request.json())
    await connections.broadcast_all({"type": "federation.config", "payload": {"is_director": federation.is_director}})
    return {"accepted": True}


@app.post("/api/federation/pair/offer")
async def create_pairing_offer(request: Request) -> dict:
    require_local_operator(request)
    try:
        code = federation.create_pairing_offer()
    except OSError as error:
        raise HTTPException(status_code=500, detail="Pairing configuration cannot be saved") from error
    query = urlencode({"v": "1", "url": discovery.advertised_url(), "code": code})
    return {"pairing_uri": f"multicam://federation?{query}", "pairing_code": code, "expires_in_seconds": 300}


@app.post("/api/federation/pair")
async def join_pairing_offer(request: Request) -> dict:
    require_local_operator(request)
    if store.active_session_id:
        raise HTTPException(status_code=409, detail="Před párováním ukončete vlastní relaci")
    body = await request.json()
    short_code = str(body.get("pairing_code", "")).replace("-", "").replace(" ", "").upper()
    if short_code:
        try:
            await federation.pair_with_discovered_peer(short_code)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Samotný kód funguje jen mezi již nalezenými pulty. "
                    "Zkopírujte z directora celý párovací odkaz multicam://federation?…"
                ),
            ) from error
        return await federation_config()
    payload = str(body.get("pairing_uri", ""))
    parsed = urlparse(payload)
    values = parse_qs(parsed.query)
    if parsed.scheme != "multicam" or parsed.netloc != "federation" or values.get("v") != ["1"]:
        raise HTTPException(status_code=400, detail="Invalid MultiCam pairing QR")
    try:
        await federation.pair_with(values["url"][0], values["code"][0])
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Pairing failed or the code expired") from error
    return await federation_config()


@app.post("/api/federation/pair/accept")
async def accept_pairing_offer(request: Request) -> dict:
    data = await request.json()
    try:
        # Validate the peer before consuming the one-time code. A malformed
        # request must not be able to burn a legitimate operator's offer.
        peer_backend_id = str(UUID(data["peer_backend_id"]))
        peer_url = str(data["peer_url"])
        if not peer_url.startswith(("http://", "https://")):
            raise ValueError("Peer URL must use HTTP or HTTPS")
        token = federation.accept_pairing(str(data.get("code", "")))
        federation.register_peer(peer_backend_id, peer_url)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "token": token, "peers": federation.membership(), **federation.assignments(),
    }


@app.post("/api/federation/register-peer")
async def register_peer(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    data = await request.json()
    try:
        federation.register_peer(str(UUID(data["backend_id"])), str(data["url"]))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"registered": True}


@app.get("/api/federation/snapshot")
async def federation_snapshot(x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    sessions = [item for item in await store.list() if item.session_id not in deleted_session_ids]
    return {
        "backend_id": discovery.backend_id,
        "sessions": [item.model_dump(mode="json") for item in sessions],
        "peers": federation.membership(),
        **federation.assignments(),
        "active_session": store.active_state(),
    }


@app.post("/api/federation/delete-session")
async def federation_delete_session(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    raise HTTPException(status_code=410, detail="Mazání relací je pouze lokální")


@app.post("/api/federation/control")
async def federation_control(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    data = await request.json()
    if data.get("director_backend_id") != federation.director_backend_id:
        raise HTTPException(status_code=409, detail="Povel nepochází od aktuálního directora")
    await apply_control(UUID(data["session_id"]), SocketMessage.model_validate(data["message"]), relay=False)
    return {"accepted": True}


@app.post("/api/federation/control-request")
async def federation_control_request(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Control requests must be handled by the director")
    data = await request.json()
    await apply_control(UUID(data["session_id"]), SocketMessage.model_validate(data["message"]), relay=True)
    return {"accepted": True}


@app.post("/api/federation/validate-role")
async def federation_validate_role(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Role validation is only available on the director")
    data = await request.json()
    session = await store.get(UUID(data["session_id"]))
    role = DeviceRole(data["role"])
    if role in {DeviceRole.MAIN_CAMERA, DeviceRole.TOP_CAMERA}:
        occupied = next((device for device in session.devices.values() if device.role == role and str(device.device_id) != data.get("device_id")), None)
        if occupied:
            raise HTTPException(status_code=409, detail=f"Role {role.value} už používá {occupied.name}")
    return {"accepted": True}


@app.post("/api/federation/event")
async def federation_event(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    data = await request.json()
    session_id = UUID(data["session_id"])
    message = SocketMessage.model_validate(data["message"])
    if federation.is_director and message.type == "control.ack" and message.payload.get("device_id"):
        ack_state = {
            "ready": DeviceState.ARMED, "started": DeviceState.RECORDING,
            "stopped": DeviceState.STORED, "error": DeviceState.READY,
        }.get(message.payload.get("status"))
        if ack_state is not None:
            try:
                await store.set_device_state(session_id, UUID(message.payload["device_id"]), ack_state)
            except (SessionNotFoundError, KeyError):
                pass
    if message.type == "clap.trigger":
        uploads.append_session_event(session_id, {"type": "clap.step", **message.payload})
    await connections.broadcast(session_id, message.model_dump(mode="json"))
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
    if not federation.transfer_enabled or not federation.is_storage:
        raise HTTPException(status_code=403, detail="Tento backend nyní nepřijímá data jako storage")
    if session_id in deleted_session_ids:
        raise HTTPException(status_code=410, detail="Relace byla na tomto úložišti lokálně smazána")
    if source_backend_id not in federation.peers:
        raise HTTPException(status_code=403, detail="Zdrojový backend není spárovaný")
    check_storage()
    uploads.root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="multicam-federation-", suffix=".zip", dir=uploads.root)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as output:
            async for chunk in request.stream():
                output.write(chunk)
        async with storage_mutation_lock:
            check_storage()
            if session_id in deleted_session_ids:
                raise HTTPException(status_code=410, detail="Relace byla lokálně smazána")
            imported = await asyncio.to_thread(import_take, uploads.root, temporary, session_id, take_id, verify_media=True)
    except MediaValidationError as error:
        diagnostics = uploads.root / ".diagnostics"
        diagnostics.mkdir(exist_ok=True)
        os.replace(temporary, diagnostics / f"{session_id}-{take_id}-{uuid4()}.invalid.zip")
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
    except BundleError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    finally:
        temporary.unlink(missing_ok=True)
    return {"verified": True, "imported_files": imported, "source_backend_id": source_backend_id}


async def merge_federation_snapshot(snapshot: dict, peer: dict) -> None:
    if snapshot["backend_id"] != peer["backend_id"]:
        raise ValueError("Identita backendu neodpovídá spárovanému uzlu")
    for backend_id, url in snapshot.get("peers", {}).items():
        federation.register_peer(backend_id, url)
    federation.adopt_assignments(snapshot)
    authoritative = snapshot["backend_id"] == federation.director_backend_id
    remote_sessions = [Session.model_validate(raw) for raw in snapshot["sessions"]]
    active_state = snapshot.get("active_session")
    active_id = UUID(active_state["session_id"]) if active_state and active_state.get("session_id") else None
    active = next((item for item in remote_sessions if item.session_id == active_id and item.state != SessionState.CLOSED), None)
    peer_active_sessions[snapshot["backend_id"]] = active.model_dump(mode="json") if active else None
    for remote in remote_sessions:
        if remote.session_id in deleted_session_ids:
            continue
        try:
            merged = await store.merge_remote(remote, snapshot["backend_id"], discovery.backend_id,
                                              authoritative=authoritative)
        except SessionNotFoundError:
            continue
        await connections.broadcast(merged.session_id, {"type": "session.updated", "payload": merged.model_dump(mode="json")})
        if authoritative and merged.state == SessionState.CLOSED:
            # STOP may have been lost while this peer was disconnected.
            if any(device.state == DeviceState.RECORDING for device in merged.devices.values()):
                await connections.broadcast(merged.session_id, {"type": "recording.stop", "payload": {"command_id": "session-closed"}})
    if authoritative:
        previous = store.active_session_id
        if previous and previous != active_id:
            await connections.broadcast(previous, {"type": "recording.stop", "payload": {"command_id": "session-closed"}})
            await store.clear_active(datetime.fromisoformat(active_state["changed_at"]), federation.director_backend_id)
        if active_id and active_id not in deleted_session_ids:
            await store.activate(active_id, federation.director_backend_id,
                                 datetime.fromisoformat(active_state["changed_at"]), force=True)
        elif active_state:
            await store.clear_active(datetime.fromisoformat(active_state["changed_at"]), federation.director_backend_id)
        if previous != store.active_session_id:
            await connections.broadcast_all({"type": "federation.active_session", "payload": {
                "session_id": str(store.active_session_id) if store.active_session_id else None}})
    if authoritative and active and active.last_control:
        await apply_control(active.session_id, SocketMessage.model_validate(active.last_control), relay=False)
    await connections.broadcast_all({"type": "federation.config", "payload": {"is_director": federation.is_director}})


async def federation_sync_loop() -> None:
    while True:
        if federation.enabled:
            failures = []
            for peer in federation.target_peers():
                try:
                    await federation.post_json(peer["url"], "/api/federation/register-peer", {
                        "backend_id": discovery.backend_id, "url": discovery.advertised_url()})
                    await merge_federation_snapshot(await federation.get_snapshot(peer["url"]), peer)
                except (OSError, ValueError, KeyError, HTTPException) as error:
                    failures.append(error)
            if failures:
                federation.mark_sync_error(failures[0])
            else:
                federation.mark_sync_ok()
        await asyncio.sleep(2)


async def federation_transfer_loop() -> None:
    # Large media transfers must not delay session/control synchronization.
    while True:
        if federation.enabled and federation.transfer_enabled:
            for peer in federation.direct_transfer_peers():
                try:
                    await sync_completed_takes(peer)
                except (OSError, ValueError, KeyError) as error:
                    federation.mark_sync_error(error)
        await asyncio.sleep(5)


@app.get("/api/hotspot")
async def hotspot_status() -> dict:
    status_path = Path(os.environ.get("MULTICAM_HOTSPOT_STATUS", "/run/multicam/hotspot.json"))
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": False}


@app.get("/api/network-interfaces")
async def network_interfaces() -> dict:
    return {"interfaces": interface_addresses()}


@app.get("/api/zerotier")
async def get_zerotier_status() -> dict:
    try:
        return zerotier_status()
    except (OSError, ZeroTierError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/zerotier/join")
async def join_zerotier_network(request: Request) -> dict:
    require_local_operator(request)
    data = await request.json()
    try:
        return join_zerotier(
            str(data.get("network_id", "")), Path(__file__).resolve().parents[2],
            bool(data.get("install", False)),
        )
    except (OSError, subprocess.SubprocessError, ZeroTierError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
async def create_session(data: SessionCreate) -> Session:
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Relace vytváří řídicí pult (director)")
    try:
        return await store.create(data)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/sessions/{session_id}/activate", response_model=Session)
async def activate_session(session_id: UUID) -> Session:
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Aktivní relaci určuje řídicí pult (director)")
    try:
        return await store.activate(session_id, discovery.backend_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/sessions/{session_id}/close", response_model=Session)
async def close_session(session_id: UUID) -> Session:
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Relaci ukončuje director")
    async with control_lock:
        try:
            session = await store.set_state(session_id, SessionState.CLOSED)
        except SessionNotFoundError as error:
            raise HTTPException(status_code=404, detail="Session not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
        await connections.broadcast_all({"type": "federation.active_session", "payload": {"session_id": None}})
        # The persisted snapshot retries closure if any peer is currently offline.
        await federation.broadcast_json("/api/federation/session-state", await federation_snapshot(federation.token) if federation.enabled else {})
        return session


@app.post("/api/federation/session-state")
async def receive_session_state(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    snapshot = await request.json()
    if snapshot.get("backend_id") != federation.director_backend_id:
        raise HTTPException(status_code=409, detail="Relaci řídí aktuální director")
    await merge_federation_snapshot(snapshot, {"backend_id": federation.director_backend_id})
    return {"accepted": True}


async def delete_session_data(session_id: UUID) -> None:
    if any(job["metadata"]["session_id"] == str(session_id) for job in ip_cameras.jobs.values()):
        raise HTTPException(status_code=409, detail="IP kamera ještě dokončuje uložení záznamu")
    try:
        await store.delete(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    deleted_session_ids.add(session_id)
    persist_deleted_sessions()
    await connections.broadcast(session_id, {"type": "session.deleted", "payload": {"session_id": str(session_id)}})


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: UUID) -> dict:
    async with storage_mutation_lock:
        await delete_session_data(session_id)
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
        current = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    if current.state == SessionState.CLOSED:
        raise HTTPException(status_code=409, detail="Relace je ukončená")
    data.device_id = data.device_id or uuid4()
    try:
        if not federation.is_director:
            await federation.send_to_director("/api/federation/register-device", {
                "session_id": str(session_id), "device": data.model_dump(mode="json"),
                "backend_id": discovery.backend_id, "backend_name": discovery.name})
        device = await store.register_device(session_id, data, discovery.backend_id, discovery.name)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail=f"Kameru nelze registrovat: {error}") from error
    await connections.broadcast(session_id, {"type": "session.updated", "payload": (await store.get(session_id)).model_dump(mode="json")})
    return device


@app.post("/api/sessions/{session_id}/ip-cameras", response_model=Device)
async def add_ip_camera(session_id: UUID, request: Request) -> Device:
    require_local_operator(request)
    data = await request.json()
    if (await store.get(session_id)).state == SessionState.RECORDING:
        raise HTTPException(status_code=409, detail="IP kameru přidejte před nahráváním")
    url = str(data.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme not in {"rtsp", "http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Použijte RTSP nebo HTTP(S) URL kamery")
    device = await register_device(session_id, DeviceRegistration(name=data.get("name", "IP kamera"), role=data.get("role", "secondary_camera")))
    ip_cameras.configure(device.device_id, url)
    device = await store.set_device_source(session_id, device.device_id, "ip_camera")
    return device


@app.post("/api/federation/register-device")
async def register_remote_device(request: Request, x_multicam_federation: str | None = Header(default=None)) -> dict:
    require_federation_token(x_multicam_federation)
    if not federation.is_director:
        raise HTTPException(status_code=409, detail="Kamery registruje director")
    data = await request.json()
    if data.get("backend_id") not in federation.peers:
        raise HTTPException(status_code=403, detail="Backend není spárovaný")
    try:
        device = await store.register_device(UUID(data["session_id"]), DeviceRegistration.model_validate(data["device"]),
                                            data["backend_id"], data.get("backend_name"))
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return device.model_dump(mode="json")


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
    session = await store.get(session_id)
    if session.state == SessionState.RECORDING:
        raise HTTPException(status_code=423, detail="Upload čeká na ukončení záznamu")
    now = time.monotonic()
    lease = upload_leases.get(session_id)
    if lease and lease[0] != device_id and now - lease[1] < 60:
        raise HTTPException(status_code=429, detail="Lokální upload právě používá jiná kamera", headers={"Retry-After": "3"})
    upload_leases[session_id] = (device_id, now)
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
    if (await store.get(session_id)).state == SessionState.RECORDING:
        raise HTTPException(status_code=423, detail="Upload čeká na ukončení záznamu")
    lease = upload_leases.get(session_id)
    if lease and lease[0] != device_id and time.monotonic() - lease[1] < 60:
        raise HTTPException(status_code=429, detail="Lokální upload právě používá jiná kamera", headers={"Retry-After": "3"})
    upload_leases[session_id] = (device_id, time.monotonic())
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
    if (await store.get(session_id)).state == SessionState.RECORDING:
        raise HTTPException(status_code=423, detail="Upload čeká na ukončení záznamu")
    await store.set_device_state(session_id, device_id, DeviceState.VALIDATING)
    try:
        receipt = await uploads.complete(session_id, device_id, upload_id)
    except UploadNotFoundError as error:
        raise HTTPException(status_code=404, detail="Upload not found") from error
    except UploadConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except MediaValidationError as error:
        await store.set_device_state(session_id, device_id, DeviceState.FAILED)
        raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
    next_state = DeviceState.VERIFIED if uploads.capture_verified(session_id, device_id, receipt.capture_id) else DeviceState.UPLOADING
    updated = await store.set_device_state(session_id, device_id, next_state)
    await connections.broadcast(session_id, {"type": "session.updated", "payload": updated.model_dump(mode="json")})
    if next_state == DeviceState.VERIFIED:
        upload_leases.pop(session_id, None)
    return receipt


def completed_local_takes(session: Session) -> dict[UUID, str]:
    captures: dict[UUID, list[str]] = {}
    for media in uploads.list_media(session):
        device = session.devices[str(media.device_id)]
        if device.owner_backend_id and device.owner_backend_id != discovery.backend_id:
            continue
        if not uploads.capture_verified(session.session_id, media.device_id, media.capture_id):
            continue
        captures.setdefault(media.take_id or media.capture_id, []).append(str(media.capture_id))
    return {take_id: hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
            for take_id, ids in captures.items()}


def transfer_receipt(session_id: UUID, take_id: UUID, peer_id: str) -> Path:
    return uploads.root / str(session_id) / ".federation-sent" / f"{take_id}-{peer_id}.json"


def transfer_is_current(receipt: Path, fingerprint: str) -> bool:
    try:
        return json.loads(receipt.read_text())["fingerprint"] == fingerprint
    except (OSError, ValueError, KeyError):
        return False


async def replicate_take_to_peer(session_id: UUID, take_id: UUID, peer: dict) -> None:
    check_storage()
    if not federation.transfer_enabled or peer["backend_id"] != federation.storage_backend_id:
        return
    session = await store.get(session_id)
    if session.state == SessionState.RECORDING:
        return
    fingerprint = completed_local_takes(session).get(take_id)
    if not fingerprint:
        return
    local_ids = {media.capture_id for media in uploads.list_media(session)
                 if (media.take_id or media.capture_id) == take_id
                 and uploads.capture_verified(session_id, media.device_id, media.capture_id)
                 and session.devices[str(media.device_id)].owner_backend_id in {None, discovery.backend_id}}
    receipt = transfer_receipt(session_id, take_id, peer["backend_id"])
    if transfer_is_current(receipt, fingerprint):
        return
    destination = uploads.root / ".federation" / f"{session_id}-{take_id}-{discovery.backend_id}.zip"
    try:
        # PUSH session metadata to storage before sending the data bundle
        # Storage uzel totiž odmítne importovat ZIP, pokud u sebe nemá založenou relaci (session.json)
        await federation.post_json(peer["url"], "/api/federation/session-state", await federation_snapshot(federation.token))
        
        await asyncio.to_thread(export_take, uploads.root, session_id, take_id, local_ids, destination)
        await federation.send_bundle(peer["url"], destination, str(session_id), str(take_id))
        if not federation.transfer_enabled:
            return
        receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps({"peer_backend_id": peer["backend_id"], "take_id": str(take_id),
                                        "fingerprint": fingerprint, "sent_at": datetime.now(timezone.utc).isoformat()}))
        os.replace(temporary, receipt)
    finally:
        destination.unlink(missing_ok=True)


async def sync_completed_takes(peer: dict) -> None:
    if not federation.transfer_enabled or peer["backend_id"] != federation.storage_backend_id:
        return
    for session in await store.list():
        if session.state == SessionState.RECORDING:
            continue
        for take_id in completed_local_takes(session):
            await replicate_take_to_peer(session.session_id, take_id, peer)


def media_with_backend(session: Session, media: CaptureMedia, *, available_locally: bool) -> CaptureMedia:
    device = session.devices.get(str(media.device_id))
    return media.model_copy(update={
        "owner_backend_id": device.owner_backend_id if device else None,
        "owner_backend_name": device.owner_backend_name if device else None,
        "available_locally": available_locally,
        "video_url": media.video_url if available_locally else None,
        "telemetry_url": media.telemetry_url if available_locally else None,
    })


@app.get("/api/federation/sessions/{session_id}/media")
async def federation_session_media(session_id: UUID, x_multicam_federation: str | None = Header(default=None)) -> list[dict]:
    require_federation_token(x_multicam_federation)
    session = await store.get(session_id)
    return [media_with_backend(session, item, available_locally=True).model_dump(mode="json") for item in uploads.list_media(session)]


@app.get("/api/sessions/{session_id}/media", response_model=list[CaptureMedia])
async def list_session_media(session_id: UUID) -> list[CaptureMedia]:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    local = [media_with_backend(session, item, available_locally=True) for item in uploads.list_media(session)]
    combined = {item.capture_id: item for item in local}
    if federation.enabled and session.state != SessionState.CLOSED:
        for peer in federation.target_peers():
            try:
                remote = await federation.get_json(peer["url"], f"/api/federation/sessions/{session_id}/media")
                for raw in remote:
                    item = CaptureMedia.model_validate(raw)
                    if item.capture_id not in combined:
                        combined[item.capture_id] = item.model_copy(update={
                            "video_url": None, "telemetry_url": None, "available_locally": False})
            except (OSError, ValueError):
                pass
    return list(combined.values())


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
    except UploadConflictError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
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
    async with control_lock:
        if relay and not federation.is_director:
            raise HTTPException(status_code=409, detail="Nahrávání řídí aktuální director")
        await _apply_control(session_id, message, relay=relay)


async def _apply_control(session_id: UUID, message: SocketMessage, *, relay: bool) -> None:
    current = await store.get(session_id)
    if not relay:
        revision = int(message.payload.get("state_revision", current.state_revision + 1))
        if revision <= applied_controls.get(session_id, -1) or revision < current.state_revision:
            return
    else:
        if current.last_control and message.payload.get("command_id") and current.last_control["payload"].get("command_id") == message.payload["command_id"]:
            return
        revision = current.state_revision + 1
        message.payload["state_revision"] = revision
    if current.state == SessionState.CLOSED or store.active_session_id != session_id:
        raise HTTPException(status_code=409, detail="Ovládat lze pouze aktuální neukončenou relaci")
    if message.type == "control.arm" and current.state == SessionState.RECORDING:
        raise HTTPException(status_code=409, detail="Nejprve zastavte nahrávání")
    if message.type == "recording.start" and current.state != SessionState.ARMED and relay:
        raise HTTPException(status_code=409, detail="Nejprve připravte relaci pomocí ARM")
    local_devices = [
        device for device in current.devices.values()
        if not device.owner_backend_id or device.owner_backend_id == discovery.backend_id
    ]
    controlled_devices = list(current.devices.values()) if federation.is_director else local_devices
    if message.type == "control.arm":
        session = await store.set_state(session_id, SessionState.ARMED, revision=revision, control=message.model_dump(mode="json"))
    elif message.type == "recording.start":
        unready = [device.name for device in controlled_devices if device.connected and device.state != DeviceState.ARMED]
        if unready and relay:
            raise HTTPException(status_code=409, detail=f"Kamery bez ARM: {', '.join(unready)}")
        message.payload.setdefault("take_id", str(uuid4()))
        session = await store.set_state(session_id, SessionState.RECORDING, revision=revision, control=message.model_dump(mode="json"))
        uploads.append_session_event(session_id, {
            "type": "recording.started", "take_id": message.payload["take_id"],
            "local_device_ids": [str(device.device_id) for device in local_devices if device.connected],
            "backend_id": discovery.backend_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif message.type == "recording.stop":
        session = await store.set_state(session_id, SessionState.STOPPED, revision=revision, control=message.model_dump(mode="json"))
    else:
        raise HTTPException(status_code=400, detail="Unsupported federation control")
    if message.type == "control.arm":
        for ack in await ip_cameras.arm(session_id):
            event = {"type": "control.ack", "payload": {**ack, "command_id": message.payload.get("command_id")}}
            await connections.broadcast(session_id, event)
            await federation.broadcast_json("/api/federation/event", {"session_id": str(session_id), "message": event})
    elif message.type == "recording.start":
        await ip_cameras.start(session_id, UUID(message.payload["take_id"]))
    elif message.type == "recording.stop":
        await ip_cameras.stop(session_id)
    session = await store.get(session_id)
    applied_controls[session_id] = revision
    await connections.broadcast(session_id, {"type": "session.updated", "payload": session.model_dump(mode="json")})
    await connections.broadcast(session_id, message.model_dump(mode="json"))
    if relay:
        task = asyncio.create_task(federation.broadcast_json("/api/federation/control", {
            "session_id": str(session_id), "message": message.model_dump(mode="json"),
            "director_backend_id": federation.director_backend_id,
        }))
        federation_tasks.add(task)
        task.add_done_callback(federation_tasks.discard)
    if message.type == "recording.start" and relay:
        asyncio.create_task(trigger_delayed_clap(session_id))


@app.websocket("/api/ws/{session_id}")
async def session_socket(websocket: WebSocket, session_id: UUID, device_id: UUID | None = None) -> None:
    try:
        session = await store.get(session_id)
    except SessionNotFoundError:
        await websocket.close(code=4404, reason="Session not found")
        return
    if device_id is not None:
        if str(device_id) not in session.devices:
            await websocket.close(code=4404, reason="Device not found")
            return
        await store.set_connected(session_id, device_id, True)
        session = await store.get(session_id)
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
                if federation.is_director:
                    asyncio.create_task(run_clap_sequence(session_id, automatic=False))
                continue
            if message.type in {"control.arm", "recording.start", "recording.stop"}:
                try:
                    if not federation.is_director:
                        await federation.send_to_director("/api/federation/control-request", {
                            "session_id": str(session_id), "message": message.model_dump(mode="json"),
                        })
                    else:
                        await apply_control(session_id, message, relay=True)
                except HTTPException as control_error:
                    await websocket.send_json({
                        "type": "control.rejected",
                        "payload": {"command_id": message.payload.get("command_id"), "detail": control_error.detail},
                    })
                except Exception:
                    await websocket.send_json({
                        "type": "control.rejected",
                        "payload": {"command_id": message.payload.get("command_id"), "detail": "Řídicí pult není dostupný."},
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
        await federation.broadcast_json("/api/federation/event", {
            "session_id": str(session_id), "message": {"type": "clap.trigger", "payload": payload}})
        await asyncio.sleep(1.1)
    uploads.append_session_event(session_id, {
        "type": "clap.sequence.completed", "sequence_id": sequence_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    active_clap_sequences.discard(session_id)


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

