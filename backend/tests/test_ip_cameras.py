import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import subprocess
import threading
from uuid import uuid4

import pytest
from app.ip_cameras import IPCameraService
from app.models import DeviceRegistration, DeviceRole, DeviceState, SessionCreate
from app.store import SessionStore
from app.uploads import UploadService


def test_http_camera_records_valid_media_and_preserves_source_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('MULTICAM_TRANSCODE', '0')
    source = tmp_path / 'camera.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=64x48:rate=10',
                    '-t', '0.5', '-c:v', 'libx264', '-movflags', '+faststart', str(source)], check=True)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SessionStore(tmp_path / 'sessions')
    uploads = UploadService(store.root)
    cameras = IPCameraService(store, uploads)
    async def run():
        session = await store.create(SessionCreate(name='IP'))
        device = await store.register_device(session.session_id, DeviceRegistration(name='HTTP camera', role=DeviceRole.SECONDARY_CAMERA))
        url = f'http://127.0.0.1:{server.server_port}/camera.mp4'
        cameras.configure(device.device_id, url)
        assert cameras.config_path().stat().st_mode & 0o777 == 0o600
        ack = await cameras.arm(session.session_id)
        assert ack[0]['status'] == 'ready'
        take_id = uuid4()
        await cameras.start(session.session_id, take_id)
        await asyncio.wait_for(asyncio.gather(*cameras.finishing), 15)
        assert not cameras.jobs
        media = uploads.list_media(await store.get(session.session_id))
        assert len(media) == 1
        assert media[0].take_id == take_id
        assert uploads.capture_verified(session.session_id, device.device_id, media[0].capture_id)
        assert (await store.get(session.session_id)).devices[str(device.device_id)].state == DeviceState.VERIFIED
        assert url not in (store.root / str(session.session_id) / 'session.json').read_text()
        job = next(store.root.glob('*/.ip-camera-jobs/*/job.json'))
        assert json.loads(job.read_text())['state'] == 'verified'
    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ip_camera_rejects_local_file_protocol(tmp_path):
    cameras = IPCameraService(SessionStore(tmp_path), UploadService(tmp_path))
    with pytest.raises(ValueError, match='RTSP'):
        cameras.configure(uuid4(), 'file:///etc/passwd')
    assert not cameras.config_path().exists()
