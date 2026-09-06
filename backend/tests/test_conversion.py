import asyncio
import hashlib
import json
import shutil
import subprocess
from uuid import uuid4

import pytest

from app.models import UploadCreate
from app.uploads import UploadConflictError, UploadService


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required')
@pytest.mark.parametrize('audio', [False, True])
def test_upload_converts_mp4_and_preserves_receipt(tmp_path, audio):
    source = tmp_path / 'input.mp4'
    command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=64x48:rate=12:duration=0.5']
    if audio:
        command += ['-f', 'lavfi', '-i', 'sine=duration=0.5']
    subprocess.run(command + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    payload = source.read_bytes()
    service = UploadService(tmp_path / 'sessions')
    session_id, device_id = uuid4(), uuid4()
    data = UploadCreate(file_name='iphone.mp4', mime_type='video/mp4', size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(), chunk_size=256 * 1024, total_chunks=1)

    async def upload():
        status = await service.create(session_id, device_id, data)
        await service.put_chunk(session_id, device_id, status.upload_id, 0, payload, data.sha256)
        receipt = await service.complete(session_id, device_id, status.upload_id)
        assert await service.complete(session_id, device_id, status.upload_id) == receipt
        return receipt

    receipt = asyncio.run(upload())
    assert (service.root / receipt.file_path).read_bytes() == payload
    assert receipt.sha256 == data.sha256
    output = service.playback_path(session_id, device_id, data.capture_id)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)],
                           capture_output=True, text=True, check=True)
    streams = json.loads(probe.stdout)['streams']
    assert [s['codec_name'] for s in streams] == (['vp8', 'opus'] if audio else ['vp8'])
    assert (streams[0]['width'], streams[0]['height']) == (64, 48)
    modified = output.stat().st_mtime_ns
    assert service.playback_path(session_id, device_id, data.capture_id).stat().st_mtime_ns == modified
    assert service.delete_capture(session_id, device_id, data.capture_id)
    assert not output.exists()


def test_conversion_failure_keeps_original(tmp_path, monkeypatch):
    source = tmp_path / 'input.mov'
    source.write_bytes(b'original')
    service = UploadService(tmp_path)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError('ffmpeg')

    monkeypatch.setattr(subprocess, 'run', unavailable)
    with pytest.raises(UploadConflictError):
        service.normalize_recording(source)
    assert source.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [source]


def test_webm_is_not_transcoded(tmp_path):
    source = tmp_path / 'android.webm'
    source.write_bytes(b'original')
    assert UploadService(tmp_path).normalize_recording(source) == source
