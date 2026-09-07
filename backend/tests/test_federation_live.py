"""Exercise three real processes so each peer has independent in-memory state."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx


def test_three_peers_storage_handover_and_local_history(tmp_path):
    project = Path(__file__).resolve().parents[2]
    ids = [str(uuid4()) for _ in range(3)]
    ports = []
    for _ in ids:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            ports.append(listener.getsockname()[1])
    urls = [f'http://127.0.0.1:{port}' for port in ports]
    token = 'integration-test-secret-' + 'x' * 32
    headers = {'X-MultiCam-Federation': token}
    processes = []
    logs = []
    roots = []
    client = httpx.Client(timeout=5, trust_env=False)

    def wait_for(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (httpx.HTTPError, KeyError):
                pass
            time.sleep(.1)
        raise AssertionError('Peer did not converge: ' + '\n'.join(path.read_text() for path in tmp_path.glob('*.log')))

    def post(node, path, payload=None, **kwargs):
        response = client.post(urls[node] + path, json=payload, **kwargs)
        assert response.is_success, response.text
        return response.json()

    try:
        for index in range(3):
            root = tmp_path / str(index)
            root.mkdir()
            roots.append(root)
            config = root / 'federation.json'
            config.write_text(json.dumps({'token': token, 'peers': dict(zip(ids, urls)),
                                          'director_backend_id': ids[0], 'storage_backend_id': ids[2],
                                          'assignment_revision': 1}))
            log = (tmp_path / f'{index}.log').open('w')
            logs.append(log)
            env = {**os.environ, 'MULTICAM_DATA_DIR': str(root / 'sessions'),
                   'MULTICAM_FEDERATION_CONFIG': str(config), 'MULTICAM_BACKEND_ID': ids[index],
                   'MULTICAM_PUBLIC_URL': urls[index], 'MULTICAM_DISCOVERY': '0',
                   'MULTICAM_TRANSCODE': '0', 'MULTICAM_REQUIRED_STORAGE_MOUNT': ''}
            processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app.main:app',
                                                '--host', '127.0.0.1', '--port', str(ports[index]),
                                                '--loop', 'asyncio'], cwd=project, env=env, stdout=log, stderr=log))
        for url in urls:
            wait_for(lambda: client.get(url + '/api/health').is_success)
        session = post(0, '/api/sessions', {'name': 'Distributed'})
        sid = session['session_id']
        for url in urls:
            wait_for(lambda: client.get(url + '/api/sessions/current').json().get('session_id') == sid)
        for kind in ['control.arm', 'recording.start', 'recording.stop']:
            post(0, '/api/federation/control-request', {'session_id': sid, 'message': {'type': kind, 'payload': {'command_id': str(uuid4())}}}, headers=headers)
        for url in urls:
            wait_for(lambda: client.get(url + '/api/sessions/current').json().get('state') == 'stopped')
        # A camera belongs to the capture peer while the final storage is a third peer.
        device = post(1, f'/api/sessions/{sid}/devices', {'name': 'Remote camera', 'role': 'main_camera'})
        assert client.post(urls[0] + f'/api/sessions/{sid}/devices', json={'name': 'Duplicate main', 'role': 'main_camera'}).status_code == 409
        capture_id, take_id = str(uuid4()), str(uuid4())
        base = f"/api/sessions/{sid}/devices/{device['device_id']}/uploads"
        for kind, payload, name, mime in [('recording', b'original-video', 'camera.webm', 'video/webm'),
                                           ('telemetry', b'{"event":"recording_started"}\n', 'timing.jsonl', 'application/json')]:
            digest = hashlib.sha256(payload).hexdigest()
            upload = post(1, base, {'capture_id': capture_id, 'take_id': take_id, 'kind': kind,
                                    'file_name': name, 'mime_type': mime, 'size_bytes': len(payload),
                                    'sha256': digest, 'chunk_size': 256 * 1024, 'total_chunks': 1})
            response = client.put(urls[1] + base + f"/{upload['upload_id']}/chunks/0", content=payload,
                                  headers={'X-Chunk-SHA256': digest})
            assert response.is_success, response.text
            post(1, base + f"/{upload['upload_id']}/complete")
        storage_video = roots[2] / 'sessions' / sid / 'devices' / device['device_id'] / 'recordings' / f'{capture_id}.webm'
        wait_for(storage_video.exists)
        assert storage_video.read_bytes() == b'original-video'
        wait_for(lambda: client.get(urls[1] + '/api/federation/transfers').json()['pending_count'] == 0)
        assert not (roots[0] / 'sessions' / sid / 'devices' / device['device_id'] / 'recordings').exists()
        post(0, f'/api/sessions/{sid}/close')
        for url in urls:
            wait_for(lambda: client.get(url + '/api/sessions/current').status_code == 404)
            assert client.post(url + f'/api/sessions/{sid}/activate').status_code == 409
        assert client.delete(urls[1] + f'/api/sessions/{sid}').is_success
        time.sleep(2.5)
        assert client.get(urls[1] + f'/api/sessions/{sid}').status_code == 404
        assert storage_video.exists()
        # Any peer can request a handover; storage remains independent.
        post(2, '/api/federation/roles', {'director_backend_id': ids[1], 'storage_backend_id': ids[2]})
        wait_for(lambda: client.get(urls[1] + '/api/federation/config').json()['is_director'])
        assert not client.get(urls[0] + '/api/federation/config').json()['is_director']
        next_session = post(1, '/api/sessions', {'name': 'Next'})
        for url in urls:
            wait_for(lambda: client.get(url + '/api/sessions/current').json().get('session_id') == next_session['session_id'])
    finally:
        client.close()
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
